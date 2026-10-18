"""`llm.run`: the platform prompt plus the use case's guidance, model and trace."""

import hashlib
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel

from app.core import events
from app.core.config import settings
from app.core.database import session_factory
from app.features.agents import llm
from app.features.use_cases import service as use_cases
from app.features.use_cases.schemas import AgentSettings
from app.main import app
from tests.support.models import down, instructions, per_role, scripted
from tests.support.users import manager


def test_http_retries_reach_the_sdk(monkeypatch):
    """`limits.http_retries`: the SDK repeats a stalled request that many times, each a whole
    timeout, before the chain moves on; unset uses the configurable platform limit."""
    monkeypatch.setenv("HELMCODE_API_KEY", "k")
    monkeypatch.setattr(settings, "agent_http_retries", 1)
    setup = llm.Setup(AgentSettings(model="helmcode:a", limits={"http_retries": 0}))
    fast = llm.chain(setup, "assistant", [])
    default = llm.chain(llm.Setup(AgentSettings(model="helmcode:a")), "assistant", [])
    assert [m.client.max_retries for m in fast.models] == [0]
    assert [m.client.max_retries for m in default.models] == [1]
    with pytest.raises(ValueError):
        AgentSettings(limits={"http_retries": 9})


def stalling_provider(monkeypatch, calls: list) -> None:
    """Helmcode on a fake transport: model "stalled" hangs until its timeout, "fast" answers."""
    import httpx2

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["model"], request.extensions["timeout"]["read"]))
        if body["model"] == "stalled":
            raise httpx2.ReadTimeout("stalled", request=request)
        message = {"role": "assistant", "content": "ok"}
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    real = llm.OpenAIProvider

    def provider(**kw):
        if "openai_client" not in kw:
            kw["http_client"] = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        return real(**kw)

    monkeypatch.setenv("HELMCODE_API_KEY", "k")
    monkeypatch.setattr(llm, "OpenAIProvider", provider)


async def test_a_stalled_request_moves_to_the_fallback_at_once(monkeypatch):
    """Latency: with `http_retries: 0` a request that times out goes to the next model after
    one timeout, and the failure is in the trace. Explicit retries remain supported."""
    fast = AgentSettings(
        model="helmcode:stalled",
        fallback_models=["helmcode:fast"],
        timeout_seconds=12,
        limits={"http_retries": 0},
    )
    calls: list = []
    stalling_provider(monkeypatch, calls)
    output, trace = await llm.run(
        Agent(None), "assistant", "hi", instructions="", setup=llm.Setup(fast)
    )
    assert (output, trace.model) == ("ok", "fast")
    assert calls == [("stalled", 12.0), ("fast", 12.0)]

    calls.clear()
    slow = fast.model_copy(update={"limits": {"http_retries": 2}})
    await llm.run(Agent(None), "assistant", "hi", instructions="", setup=llm.Setup(slow))
    assert [model for model, _ in calls] == ["stalled", "stalled", "stalled", "fast"]


async def test_platform_defaults_bound_unconfigured_provider_and_preserve_fallback(monkeypatch):
    monkeypatch.setattr(settings, "agent_timeout_seconds", 7)
    monkeypatch.setattr(settings, "agent_http_retries", 0)
    calls = []
    stalling_provider(monkeypatch, calls)
    setup = llm.Setup(AgentSettings(model="helmcode:stalled", fallback_models=["helmcode:fast"]))
    result, trace = await llm.run(Agent(None), "assistant", "hi", instructions="", setup=setup)
    assert (result, trace.model) == ("ok", "fast")
    assert calls == [("stalled", 7.0), ("fast", 7.0)]


@pytest.mark.parametrize(
    "role_timeout,model_timeout,expected",
    [
        (None, None, 120),
        (None, 3, 3),
        (5, 3, 5),
        (5, None, 5),
    ],
)
async def test_provider_timeout_overrides_remain_effective(
    monkeypatch, role_timeout, model_timeout, expected
):
    monkeypatch.setattr(settings, "agent_timeout_seconds", 120)
    calls = []
    stalling_provider(monkeypatch, calls)
    setup = llm.Setup(
        AgentSettings(
            model="helmcode:fast",
            timeout_seconds=role_timeout,
            model_settings={} if model_timeout is None else {"timeout": model_timeout},
        )
    )
    await llm.run(Agent(None), "assistant", "hi", instructions="", setup=setup)
    assert calls == [("fast", expected)]


class Answer(BaseModel):
    text: str


agent = Agent(None, output_type=Answer)
SETUP = llm.Setup(AgentSettings(instructions="Amounts in cents."), config_id=42)


async def test_guidance_is_appended_and_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(llm, "model_for", per_role({"compiler": [{"text": "ok"}]}, seen))

    output, trace = await llm.run(agent, "compiler", "hi", instructions="PLATFORM", setup=SETUP)

    effective = "PLATFORM\n\n## Guidance for this use case\nAmounts in cents."
    assert output == Answer(text="ok")
    assert instructions(seen["compiler"][0]) == effective
    assert trace.config_id == 42
    assert trace.prompt_hash == hashlib.sha256(effective.encode()).hexdigest()[:12]


async def test_the_setup_model_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def default(role: str):
        raise AssertionError("the default model must not be used")

    monkeypatch.setattr(llm, "model_for", default)
    setup = llm.Setup(AgentSettings(model="test"))

    _, trace = await llm.run(agent, "compiler", "hi", instructions="PLATFORM", setup=setup)

    assert trace.model == "test"


def chain(monkeypatch: pytest.MonkeyPatch, *models) -> llm.Setup:
    """A setup whose model and fallbacks are the scripted `models`, in order."""
    by_name = {m.model_name: m for m in models}
    monkeypatch.setattr(llm, "resolve", lambda name: by_name[name])
    primary, *fallbacks = by_name
    return llm.Setup(AgentSettings(model=primary, fallback_models=fallbacks))


async def run_traced(run_agent: Agent, setup: llm.Setup) -> tuple[object, dict]:
    """The run's output (or its AgentError) and the data of its `llm_run` span."""
    with events.span("test") as outer:
        try:
            output = await llm.run(run_agent, "compiler", "hi", instructions="P", setup=setup)
        except llm.AgentError as e:
            output = e
    return output, next(r["data"] for r in outer.rows if r["step"] == "llm_run")


async def test_a_provider_failure_moves_to_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = chain(monkeypatch, down("primary"), scripted([{"text": "ok"}], name="backup"))

    (output, trace), span = await run_traced(agent, setup)

    assert output == Answer(text="ok")
    assert trace.model == span["model"] == "backup"
    assert span["chain"] == ["primary", "backup"]
    assert [f["model"] for f in span["failed_attempts"]] == ["primary"]
    assert "status_code: 503" in span["failed_attempts"][0]["error"]


async def test_an_answer_cut_by_the_token_limit_moves_to_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cut(messages, info) -> ModelResponse:
        return ModelResponse(parts=[], finish_reason="length")

    primary = FunctionModel(cut, model_name="primary")
    setup = chain(monkeypatch, primary, scripted([{"text": "ok"}], name="backup"))

    (output, trace), span = await run_traced(agent, setup)

    assert output == Answer(text="ok")
    assert trace.model == "backup"
    assert span["failed_attempts"] == [{"model": "primary", "error": "output token limit hit"}]


picky = Agent(None, output_type=Answer)


@picky.output_validator
def _not_bad(answer: Answer) -> Answer:
    if answer.text == "bad":
        raise ModelRetry("say ok")
    return answer


async def test_a_rejected_answer_is_retried_on_the_same_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = scripted([{"text": "bad"}, {"text": "ok"}], name="primary")
    setup = chain(monkeypatch, primary, down("backup"))

    (output, trace), span = await run_traced(picky, setup)

    assert output == Answer(text="ok")
    assert (trace.model, trace.retries) == ("primary", 1)
    assert span["failed_attempts"] == []


async def test_when_every_model_fails_the_run_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = chain(monkeypatch, down("primary"), down("backup"))

    error, span = await run_traced(agent, setup)

    assert isinstance(error, llm.AgentError) and error.status_code == 502
    assert "every model failed: primary: " in error.message and "; backup: " in error.message
    assert [f["model"] for f in span["failed_attempts"]] == ["primary", "backup"]


@pytest.mark.parametrize("model", ["helmcode:any", "openai:gpt-4o", "anthropic:claude-x"])
async def test_a_provider_with_no_key_is_an_llm_error(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("HELMCODE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    setup = llm.Setup(AgentSettings(model=model, fallback_models=[model]))

    with pytest.raises(llm.AgentError, match="model not configured") as error:
        await llm.run(agent, "compiler", "hi", instructions="PLATFORM", setup=setup)

    assert error.value.status_code == 502 and error.value.code == "llm_error"


async def test_a_rule_whose_models_all_fail_stays_a_draft_with_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the API: the use case's chain is used and a compilation it cannot serve
    leaves the rule `blocked` (it escalates, ADR 0020), never stuck in `compiling`."""
    monkeypatch.setattr(llm, "resolve", lambda name: down(name))
    suffix = uuid.uuid4().hex[:8]
    definition = {
        "name": f"fallback-{suffix}",
        "decision_types": [
            {"name": "ESCALATE", "priority": 2, "requires_human": True},
            {"name": "PAY", "priority": 1, "is_default": True},
        ],
        "symbols": [{"name": "amount", "type": "number"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
        await manager(api)
        r = await api.post("/processes/definition", json=definition)
        assert r.status_code == 200, r.text
        process = r.json()["process"]
        chain_settings = AgentSettings(model="primary", fallback_models=["backup"])
        async with session_factory() as session:
            for role in ("tester", "compiler"):
                await use_cases.configure(
                    session, process["use_case_id"], role, chain_settings, "test", None
                )
        from app.features.versions import service as versions
        from app.features.versions.schemas import DraftIn

        async with session_factory() as session:
            draft = await versions.get_draft(session, process["id"])
            await versions.edit(
                session,
                process["id"],
                DraftIn(expected_revision=draft.revision, refresh_agents=True),
                "test",
            )
        r = await api.post(
            f"/processes/{process['id']}/rules",
            json={"text": "amount > 1000", "type": "prohibition", "decision": "ESCALATE"},
        )
        assert r.status_code == 201, r.text
        rule = (await api.get(f"/rules/{r.json()['id']}")).json()

    assert rule["status"] == "draft"
    assert rule["report"]["valid"] is False
    assert "every model failed: primary: " in rule["report"]["error"]
    assert "; backup: " in rule["report"]["error"]


def test_the_default_model_brings_the_default_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "resolve", lambda n: FunctionModel(lambda *_: None, model_name=n))

    def names(setup: llm.Setup) -> list[str]:
        return [m.model_name for m in llm.chain(setup, "compiler", []).models]

    assert names(llm.Setup()) == [settings.compiler_model, *settings.fallback_models]
    assert names(llm.Setup(AgentSettings(model="own"))) == ["own"]


class _Usage:
    """A PydanticAI usage whose provider publishes no price."""

    input_tokens = 1_000_000
    output_tokens = 1_000_000
    cache_read_tokens = 400_000

    def cost(self):
        raise LookupError("unknown model")


def test_a_run_on_a_provider_without_a_price_is_costed_at_the_list_price() -> None:
    """Helmcode bills a flat monthly fee and publishes no per-token rate, so PydanticAI
    cannot price a run on it. The underlying model's public rate answers what the tokens
    would cost, and cached input is charged at the cache rate."""
    usage = _Usage()
    expected = (600_000 * 0.30 + 400_000 * 0.006 + 1_000_000 * 1.20) / 1_000_000
    assert llm._cost(usage, "deepseek/deepseek-v4.1-flash") == pytest.approx(expected)
    assert llm._cost(usage, "deepseek-v4.1-flash") == pytest.approx(expected)
    assert llm._cost(usage, "a-model-nobody-published") is None
