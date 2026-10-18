"""Journaled visual provider chain with distinct reader identities."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

from app.common import prompts
from app.common.extraction import TextLine
from app.common.normalization import clean_text
from app.features.ingestion.config import Settings

from .errors import ProviderUnavailable, note_provider_failure, provider_on_cooldown
from .gemini import GENERATION, PROMPT, generate, output_text
from .generation import chat_generation
from .http import ProviderHTTP
from .journal import record_response, recorded_call, retry_after
from .transcript import remote_lines, transcript_warnings

PROMPTS = Path(__file__).parents[1] / "prompts"
COMPATIBLE_PROMPT = prompts.read(PROMPTS, "transcribe-compatible")
SCHEMA_PROMPT = prompts.read(PROMPTS, "transcribe-schema")


def _prompt(fields):
    """The requested fields identify regions to transcribe, never values to invent."""
    if fields is None:
        return COMPATIBLE_PROMPT
    return SCHEMA_PROMPT.replace("{fields}", json.dumps(fields, ensure_ascii=False))


class VisionFallback:
    """Configured image readers; downstream code must corroborate their output."""

    def __init__(self, settings: Settings, *, http=None):
        self.settings = settings
        self.http = http or ProviderHTTP()

    @property
    def independent_readers(self):
        return len({model.rsplit("/", 1)[-1].lower() for _, model in self.settings.visual_chain()})

    @property
    def configured(self):
        return bool(self.settings.visual_chain())

    def signature(self):
        first = self.settings.visual_chain()[0] if self.configured else (None, None)
        return {
            "model": first[1],
            "endpoint": self.settings.vlm_url if first[0] == "compatible" else None,
            "chain": [
                {
                    "provider": provider,
                    "model": model,
                    "endpoint": self._endpoint(provider, model),
                    "prompt": PROMPT if provider == "gemini" else COMPATIBLE_PROMPT,
                    "generation": {**GENERATION, "maxOutputTokens": self.settings.vision_max_tokens}
                    if provider == "gemini"
                    else self._generation(provider, model),
                }
                for provider, model in self.settings.visual_chain()
            ],
            "configured": self.configured,
        }

    def _endpoint(self, provider, model):
        if provider == "gemini":
            return (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            )
        base = self.settings.vlm_url if provider == "compatible" else self.settings.helmcode_url
        return base.rstrip("/") + "/chat/completions"

    def _generation(self, provider, model):
        result = chat_generation(model, self.settings.vision_max_tokens)
        if provider == "helmcode":
            result["reasoning_effort"] = "none"
        return result

    def transcribe(self, png: bytes, page: int, point_size: tuple[float, float], *, fields=None):
        """Legacy hybrid path: return the first successful reader's lines."""
        namespace = str(self.settings.data_dir)
        for index, (provider, model) in enumerate(self.settings.visual_chain()):
            if provider_on_cooldown(provider, model, namespace):
                continue
            try:
                return self._read(
                    provider, model, png, page, point_size, fields=fields, fallback=index > 0
                )
            except ProviderUnavailable as exc:
                note_provider_failure(provider, model, namespace, exc)
                continue
        raise ProviderUnavailable("No configured image reader succeeded")

    def transcribe_readers(
        self, png: bytes, page: int, point_size: tuple[float, float], *, fields=None
    ) -> dict[str, list[TextLine]]:
        """Collect at most two distinct successful provider/model visual readings."""
        readers = {}
        successful_models = set()
        namespace = str(self.settings.data_dir)
        pending = list(enumerate(self.settings.visual_chain()))

        def read(index, provider, model):
            try:
                return self._read(
                    provider, model, png, page, point_size, fields=fields, fallback=index > 0
                )
            except ProviderUnavailable as exc:
                note_provider_failure(provider, model, namespace, exc)
                return []

        # Two independent readers at a time. Failed readers can be replaced by
        # later entries; wrappers around the same model never gain another vote.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="vision") as executor:
            while pending and len(readers) < 2:
                batch, deferred = [], []
                selected = set(successful_models)
                for index, (provider, model) in pending:
                    nominal = model.rsplit("/", 1)[-1].lower()
                    if nominal in successful_models or provider_on_cooldown(
                        provider, model, namespace
                    ):
                        continue
                    if nominal in selected or len(batch) >= 2 - len(readers):
                        deferred.append((index, (provider, model)))
                        continue
                    selected.add(nominal)
                    future = executor.submit(copy_context().run, read, index, provider, model)
                    batch.append((provider, model, nominal, future))
                pending = deferred
                if not batch:
                    break
                for provider, model, nominal, future in batch:
                    lines = future.result()
                    if lines:
                        readers[f"visual:{provider}:{model}"] = lines
                        successful_models.add(nominal)
        if not readers:
            raise ProviderUnavailable("No configured image reader succeeded")
        return readers

    def _read(self, provider, model, png, page, point_size, *, fields, fallback=False):
        prompt = _prompt(fields)
        generation = self._generation(provider, model)
        endpoint = self._endpoint(provider, model)
        if provider == "gemini":
            prompt = PROMPT if fields is None else prompt
            generation = {**GENERATION, "maxOutputTokens": self.settings.vision_max_tokens}

            def call(mark_network_attempt):
                with self.http.session(self.settings.vlm_timeout) as client:
                    response = generate(
                        client,
                        model,
                        self.settings.gemini_api_key,
                        [png],
                        before_request=mark_network_attempt,
                        max_tokens=self.settings.vision_max_tokens,
                        **({"prompt": prompt} if fields is not None else {}),
                    )
                content = output_text(response)
                if transcript_warnings(content):
                    raise ValueError("Invalid visual transcript")
                return response

            result = recorded_call(
                self.settings.data_dir / "provider-journal" / "gemini",
                {
                    "endpoint": endpoint,
                    "model": model,
                    "prompt": prompt,
                    "generation": generation,
                    "image": hashlib.sha256(png).hexdigest(),
                },
                call,
                provider="gemini",
                model=model,
                operation="image_transcription",
                fallback=fallback,
                force=self.settings.ocr_force_recompute,
            )
            content = output_text(result)
        else:
            token = (
                self.settings.vlm_api_key
                if provider == "compatible"
                else self.settings.helmcode_api_key
            )
            headers = {"Authorization": "Bearer " + token} if token else {}
            body = {
                "model": model,
                **generation,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + base64.b64encode(png).decode()
                                },
                            },
                        ],
                    }
                ],
            }

            def call(mark_network_attempt):
                with self.http.session(self.settings.vlm_timeout) as client:
                    mark_network_attempt()
                    response = client.post(endpoint, json=body, headers=headers)
                trace_provider = "vision" if provider == "compatible" else provider
                record_response(
                    trace_provider, response.status_code, retry_after_s=retry_after(response)
                )
                if not response.is_success:
                    raise ProviderUnavailable(
                        f"Image provider returned HTTP {response.status_code}"
                    )
                data = response.json()
                record_response(trace_provider, response.status_code, data)
                choice = data["choices"][0]
                if choice.get("finish_reason", "stop") != "stop":
                    raise ValueError("Incomplete visual transcript")
                content = choice["message"]["content"]
                if (
                    not isinstance(content, str)
                    or not content.strip()
                    or len(content) > 50000
                    or transcript_warnings(content)
                ):
                    raise ValueError("Invalid visual transcript")
                return {"text": content, "usage": data.get("usage", {})}

            trace_provider = "vision" if provider == "compatible" else provider
            result = recorded_call(
                self.settings.data_dir / "provider-journal" / trace_provider,
                {
                    "endpoint": endpoint,
                    "model": model,
                    "prompt": prompt,
                    "generation": generation,
                    "image": hashlib.sha256(png).hexdigest(),
                },
                call,
                provider=trace_provider,
                model=model,
                operation="image_transcription",
                fallback=fallback,
                force=self.settings.ocr_force_recompute,
            )
            content = result["text"]
        if not isinstance(content, str) or not content.strip() or transcript_warnings(content):
            raise ProviderUnavailable("Invalid visual transcript")
        if provider == "gemini" and fields is None:
            return [
                line.model_copy(update={"id": f"p{page}:visual:{provider}:{model}:{index}"})
                for index, line in enumerate(remote_lines(content, page, point_size))
            ]
        return [
            TextLine(
                id=f"p{page}:visual:{provider}:{model}:{index}",
                page=page,
                raw=line,
                text=clean_text(line),
                bbox=[0, 0, *point_size],
                method="vlm",
            )
            for index, line in enumerate(content.splitlines())
            if line.strip()
        ]
