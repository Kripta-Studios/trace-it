"""Every agent runs on PydanticAI (ADR 0006): typed output, bounded retries, one seam.

What an agent does is not in the code (ADR 0011). Its platform prompt is a file under
`prompts/` (the contract, the same for every use case); how it works in a use case (model,
domain guidance, model settings, limits, examples) is a versioned `AgentConfig` of that use
case, passed here as a `Setup`. `run` joins both, records the config and prompt hash with
what the model did, and turns every failure into one error the API answers with. Tests swap
the model by monkeypatching `model_for`: no network.
"""

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAIError
from pydantic import BaseModel
from pydantic_ai import Agent, AgentRunError, capture_run_messages
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelAPIError, UserError
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.common import prompts
from app.common.exceptions import TraceError
from app.core import events
from app.core.config import settings
from app.features.ingestion.ocr.pricing import cost_snapshot
from app.features.use_cases.schemas import AgentSettings

PROMPTS = Path(__file__).parent / "prompts"
TRUNCATED = "output token limit hit"  # a `failed_attempts` error: the answer was cut


class AgentError(TraceError):
    """The model failed, or gave nothing valid within the retries. Nothing is stored."""

    status_code = 502
    code = "llm_error"


@dataclass(frozen=True)
class Trace:
    """What one agent run cost (in tokens), as its `llm_run` span records it."""

    role: str
    model: str
    requests: int
    retries: int  # answers the validators rejected and the model was asked to fix
    input_tokens: int
    output_tokens: int
    cost: float | None  # USD: the provider's price, else the model's public list price
    latency_ms: int
    config_id: int | None = None  # the AgentConfig version it ran with (None: defaults)
    prompt_hash: str = ""  # sha256[:12] of the effective instructions
    agent: str = ""  # the agent's name: tester, compiler, reviewer, normalizer, assistant
    cached_tokens: int = 0  # input tokens the provider served from its prompt cache

    def as_data(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "agent": self.agent,
            "model": self.model,
            "config_id": self.config_id,
            "prompt_hash": self.prompt_hash,
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass(frozen=True)
class Setup:
    """How a role runs in a use case: the active AgentConfig version, or the defaults."""

    settings: AgentSettings = field(default_factory=AgentSettings)
    config_id: int | None = None

    local_only: bool = False
    local_endpoint: str | None = None
    execution_hash: str | None = None

    def limit(self, name: str, default: float) -> float:
        return self.settings.limits.get(name, default)


def prompt(*names: str) -> str:
    """The platform prompts `prompts/<name>.md`, joined."""
    return prompts.read(PROMPTS, *names)


def model_for(role: str) -> Model | str:
    """The model configured for a role (`TRACE_<ROLE>_MODEL`)."""
    return getattr(settings, f"{role}_model")


def _openai(base_url: str | None, api_key: str | None, retries: float | None) -> OpenAIProvider:
    """An OpenAI-compatible provider. `retries`: how often the SDK repeats a failed request
    (timeout, 429, 5xx) before the chain moves on; None uses the platform limit. Each repeat
    waits a whole timeout again, unseen by `failed_attempts`."""
    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    if retries is None:
        retries = settings.agent_http_retries
    return OpenAIProvider(openai_client=provider.client.with_options(max_retries=int(retries)))


def resolve(
    model: Model | str, local_endpoint: str | None = None, retries: float | None = None
) -> Model | str:
    """`helmcode:<model>` runs on Helmcode's OpenAI-compatible API (EU inference, key in
    `HELMCODE_API_KEY`); any other `provider:model` string goes to PydanticAI as is.
    `retries`: the SDK's own repeats of one request (`limits.http_retries`)."""
    if isinstance(model, str) and model.startswith("local:"):
        from app.core.api_boundary import endpoint

        endpoint(local_endpoint, settings.local_base_url)
        return OpenAIChatModel(
            model.removeprefix("local:"),
            provider=_openai(
                local_endpoint or settings.local_base_url,
                os.environ.get("LOCAL_LLM_API_KEY") or "local",
                retries,
            ),
        )
    if isinstance(model, str) and model.startswith("helmcode:"):
        provider = _openai(settings.helmcode_base_url, os.environ.get("HELMCODE_API_KEY"), retries)
        return OpenAIChatModel(model.removeprefix("helmcode:"), provider=provider)
    if isinstance(model, str) and model.startswith("vercel:"):
        provider = _openai(
            settings.ai_gateway_base_url, os.environ.get("AI_GATEWAY_API_KEY"), retries
        )
        return OpenAIChatModel(model.removeprefix("vercel:"), provider=provider)
    return model


def _models(setup: Setup, role: str) -> list[Model | str]:
    own = setup.settings
    fallbacks = own.fallback_models or ([] if own.model else settings.fallback_models)
    return [own.model or model_for(role), *fallbacks]


def _price(setup: Setup, role: str, failed: list, usage: Any) -> dict[str, Any]:
    """The cost of a run, priced like the ingestion providers (`ocr/pricing.py`) for the
    model of the chain that answered (each entry of `failed` moved one model on): its
    `cost_status` is `known`/`included` with `cost_usd`, or `unknown`. Helmcode stays
    unpriced until `TRACEPAY_HELMCODE_BILLING_MODE` says how it bills."""
    models = _models(setup, role)
    spec = models[len(failed)] if len(failed) < len(models) else None
    provider, model = spec.split(":", 1) if isinstance(spec, str) and ":" in spec else (None, None)
    tokens = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": usage.cache_read_tokens,
    }
    return {"provider": provider, **cost_snapshot(provider, model, tokens)}


def chain(setup: Setup, role: str, failed: list[dict[str, str]]) -> FallbackModel:
    """The role's model, then its `fallback_models` in order (ADR 0019). A provider failure
    (`ModelAPIError`: 4xx/5xx, 429 after the SDK's retries, timeout, connection) or an
    answer cut by the output-token limit (`finish_reason == "length"`) moves on to the next
    model; each one is appended to `failed` for the trace."""

    def on_failure(error: Exception) -> bool:
        if not isinstance(error, ModelAPIError):
            return False
        failed.append({"model": error.model_name, "error": f"{type(error).__name__}: {error}"})
        return True

    def truncated(response: ModelResponse) -> bool:
        if response.finish_reason != "length":
            return False
        failed.append({"model": response.model_name or "", "error": TRUNCATED})
        return True

    models = _models(setup, role)
    retries = setup.settings.limits.get("http_retries")
    extra = () if retries is None else (retries,)
    if setup.local_only and any(
        not isinstance(model, str) or not model.startswith("local:") for model in models
    ):
        raise AgentError(f"{role}: local-only execution cannot call a hosted model")
    return FallbackModel(
        *(
            resolve(model, setup.local_endpoint, *extra)
            if isinstance(model, str) and model.startswith("local:")
            else resolve(model, *((None, *extra) if extra else ()))
            for model in models
        ),
        fallback_on=[on_failure, truncated],
    )


def _retry_prompts(messages: list[ModelMessage]) -> list[str]:
    """What the output validators sent back to the model to fix, in order."""
    return [p.model_response() for m in messages for p in m.parts if isinstance(p, RetryPromptPart)]


def _spent(messages: list[ModelMessage]) -> dict[str, int]:
    """What the model calls of a failed run cost, from the answers that came back."""
    answers = [m for m in messages if isinstance(m, ModelResponse)]
    return {
        "requests": len(answers),
        "input_tokens": sum(m.usage.input_tokens for m in answers),
        "output_tokens": sum(m.usage.output_tokens for m in answers),
        "cached_tokens": sum(m.usage.cache_read_tokens for m in answers),
    }


# Standard list prices of the models Helmcode serves, USD per million tokens as
# (input, output, cached input), read 19 September 2026 from benchlm.ai/deepseek/api-pricing.
# Helmcode bills a flat monthly fee per API key and publishes no per-token rate
# (helmcode.com/pricing), so PydanticAI cannot price a run on it. Pricing its tokens at the
# underlying model's public rate answers the question a cost column is asked: what this work
# costs per token. It is not our invoice, which is the subscription (docs/scale-and-cost.md).
_RATES = {
    "deepseek-v4.1-flash": (0.30, 1.20, 0.006),
    "deepseek-v4-flash": (0.14, 0.28, 0.0028),
}


def _cost(usage: Any, model: str) -> float | None:
    """USD for one run: the provider's own price, else the model's public list price."""
    try:
        cost = usage.cost()
        return float(getattr(cost, "total_price", cost))
    except Exception:  # noqa: BLE001 - the provider has no price; fall back to the list
        pass
    rates = _RATES.get(model.rsplit("/", 1)[-1])
    if rates is None:
        return None
    per_input, per_output, per_cached = rates
    cached = min(usage.cache_read_tokens, usage.input_tokens)
    return (
        (usage.input_tokens - cached) * per_input
        + cached * per_cached
        + usage.output_tokens * per_output
    ) / 1_000_000


async def run(
    agent: Agent,
    role: str,
    user_prompt: str,
    *,
    instructions: str,
    setup: Setup | None = None,
    deps: Any = None,
    instance_id: int | None = None,
) -> tuple[Any, Trace]:
    """One agent run for `role`: the platform `instructions` plus the use case's guidance,
    with the use case's model and settings. Returns the validated output and its trace.
    `instance_id` links the `llm_run` span to the case it is about (its trace shows it)."""
    setup = setup or Setup()
    if setup.settings.instructions:
        instructions += "\n\n## Guidance for this use case\n" + setup.settings.instructions
    failed: list[dict[str, str]] = []
    try:
        model = chain(setup, role, failed)
    except (OpenAIError, UserError) as error:  # building a provider: no key, unknown model
        raise AgentError(f"{role}: model not configured: {error}") from error
    model_settings = dict(setup.settings.model_settings)
    model_settings.setdefault("timeout", settings.agent_timeout_seconds)
    if setup.settings.timeout_seconds:
        model_settings["timeout"] = setup.settings.timeout_seconds
    prompt_hash = hashlib.sha256(instructions.encode()).hexdigest()[:12]
    with events.span(
        "llm_run",
        instance_id=instance_id,
        role=role,
        agent=agent.name or "",
        model=model.models[0].model_name,  # the answering model replaces it
        chain=[m.model_name for m in model.models],
        config_id=setup.config_id,
        execution_hash=setup.execution_hash,
        local_endpoint=setup.local_endpoint,
        model_settings=model_settings,
        prompt_hash=prompt_hash,
        # Exactly what the model saw (ADR 0018): instructions with the use case's guidance and
        # examples, and the case in the user message.
        instructions=instructions,
        user_prompt=user_prompt,
    ) as span:
        start = time.perf_counter()
        with capture_run_messages() as messages:
            try:
                result = await agent.run(
                    user_prompt,
                    model=model,
                    deps=deps,
                    instructions=instructions,
                    model_settings=model_settings or None,
                    retries=setup.settings.retries,
                    usage_limits=UsageLimits(request_limit=setup.settings.request_limit)
                    if setup.settings.request_limit is not None
                    else None,
                )
            except (AgentRunError, FallbackExceptionGroup, TimeoutError) as error:
                span.set(
                    retry_prompts=_retry_prompts(messages),
                    failed_attempts=failed,
                    **_spent(messages),
                )
                if isinstance(error, FallbackExceptionGroup):
                    tried = "; ".join(f"{f['model']}: {f['error']}" for f in failed)
                    raise AgentError(f"{role}: every model failed: {tried}") from error
                raise AgentError(f"{role}: {type(error).__name__}: {error}") from error
        usage = result.usage
        retry_prompts = _retry_prompts(messages)
        output = result.output
        span.set(
            output=output.model_dump(mode="json") if isinstance(output, BaseModel) else output,
            retry_prompts=retry_prompts,
            failed_attempts=failed,  # provider failures before the model that answered
        )
        retries = len(retry_prompts)
        answered = result.response.model_name or model.models[0].model_name
        trace = Trace(
            role=role,
            model=answered,
            requests=usage.requests,
            retries=retries,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost=_cost(usage, answered),
            latency_ms=int((time.perf_counter() - start) * 1000),
            config_id=setup.config_id,
            prompt_hash=prompt_hash,
            agent=agent.name or "",
            cached_tokens=usage.cache_read_tokens,
        )
        span.set(
            **trace.as_data(),
            **_price(setup, role, failed, usage),
            cost=trace.cost,
            latency_ms=trace.latency_ms,
        )
    return result.output, trace
