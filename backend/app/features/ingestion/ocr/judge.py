"""Jev selects among existing textual candidates; it never supplies a visual vote."""

import json
from pathlib import Path

from app.common import prompts

from .errors import ProviderUnavailable, note_provider_failure, provider_on_cooldown
from .generation import chat_generation
from .http import ProviderHTTP
from .journal import record_response, recorded_call, retry_after

PROMPTS = Path(__file__).parents[1] / "prompts"
URL = "https://api.typesafe.ai/v1/systemone"
INSTRUCTIONS = prompts.read(PROMPTS, "text-judge")


class TextJudge:
    def __init__(self, settings, *, http=None):
        self.settings = settings
        self.http = http or ProviderHTTP()

    @property
    def configured(self):
        return bool(self.settings.judge_url and self.settings.jev_model) or bool(self._chain())

    def _chain(self):
        return [
            provider
            for provider in self.settings.text_providers
            if (provider == "jev" and self.settings.jev_api_key and self.settings.jev_model)
            or (provider == "compatible" and self.settings.text_url and self.settings.text_model)
            or (
                provider == "helmcode"
                and self.settings.helmcode_api_key
                and self.settings.helmcode_text_model
            )
        ]

    def signature(self):
        if self.settings.judge_url:
            return {
                "endpoint": self.settings.judge_url,
                "model": self.settings.jev_model,
                "max_tokens": self.settings.judge_max_tokens,
                "instructions": INSTRUCTIONS,
            }
        return {
            "chain": [
                {
                    "provider": provider,
                    "endpoint": URL if provider == "jev" else self._chat_config(provider)[0],
                    "model": self.settings.jev_model
                    if provider == "jev"
                    else self._chat_config(provider)[1],
                    "generation": None if provider == "jev" else self._chat_generation(provider),
                }
                for provider in self._chain()
            ],
            "instructions": INSTRUCTIONS,
            "configured": self.configured,
        }

    def select(self, readers, fields):
        questions = {}
        for name, field in fields.items():
            if field.status == "OBSERVED":
                continue
            values = sorted({c.value for c in field.candidates if c.value and not c.error})
            if values:
                questions[name] = {
                    "type": "choice",
                    "instructions": INSTRUCTIONS + f" Requested field: {name}.",
                    "criteria": {
                        **{value: f"Select exactly {value}." for value in values},
                        "none": "No candidate is sufficiently supported by the transcripts.",
                    },
                }
        if not questions:
            return {}
        payload = {
            "model": self.settings.jev_model,
            "state": {
                "readers": {name: [line.text for line in lines] for name, lines in readers.items()}
            },
            "questions": questions,
        }

        if self.settings.judge_url:
            return self._compatible(payload, questions)

        failure = None
        namespace = str(self.settings.data_dir)
        for index, provider in enumerate(self._chain()):
            model = self.settings.jev_model if provider == "jev" else self._chat_config(provider)[1]
            if provider_on_cooldown(provider, model, namespace):
                continue
            try:
                data = (
                    self._select_jev(payload, fallback=index > 0)
                    if provider == "jev"
                    else self._select_chat(provider, payload, questions, fallback=index > 0)
                )
                break
            except ProviderUnavailable as exc:
                note_provider_failure(provider, model, namespace, exc)
                failure = exc
                continue
        else:
            raise failure or ProviderUnavailable("No text judge succeeded")
        return {
            "role": "textual_recommendation_only",
            "visual_vote": False,
            "model": data.get("model", self.settings.jev_model),
            "usage": data.get("usage", {}),
            "answers": data["answers"],
        }

    def _select_jev(self, payload, *, fallback=False):
        questions = payload["questions"]

        def call(mark_network_attempt):
            with self.http.session(self.settings.judge_timeout) as client:
                mark_network_attempt()
                response = client.post(
                    URL,
                    headers={"Authorization": "Bearer " + self.settings.jev_api_key},
                    json=payload,
                )
            record_response("jev", response.status_code, retry_after_s=retry_after(response))
            if not response.is_success:
                raise RuntimeError(f"Jev returned HTTP {response.status_code}")
            data = response.json()
            record_response("jev", response.status_code, data)
            if set(data["answers"]) != set(questions):
                raise ValueError("Jev omitted or added questions")
            for name, answer in data["answers"].items():
                choices = questions[name]["criteria"]
                probabilities = answer["probabilities"]
                if (
                    answer["type"] != "choice"
                    or answer["choice"] not in choices
                    or set(probabilities) != set(choices)
                    or not all(0 <= p <= 1 for p in probabilities.values())
                    or abs(sum(probabilities.values()) - 1) > 0.01
                    or not 0 <= answer["confidence"] <= 1
                ):
                    raise ValueError("Invalid Jev selection")
            return data

        return recorded_call(
            self.settings.data_dir / "provider-journal" / "jev",
            {"endpoint": URL, "payload": payload},
            call,
            provider="jev",
            model=self.settings.jev_model,
            operation="text_selection",
            fallback=fallback,
            force=self.settings.ocr_force_recompute,
        )

    def _chat_config(self, provider):
        if provider == "compatible":
            return (
                self.settings.text_url.rstrip("/") + "/chat/completions",
                self.settings.text_model,
                self.settings.text_api_key,
            )
        return (
            self.settings.helmcode_url.rstrip("/") + "/chat/completions",
            self.settings.helmcode_text_model,
            self.settings.helmcode_api_key,
        )

    def _chat_generation(self, provider):
        model = self._chat_config(provider)[1]
        generation = chat_generation(model, self.settings.judge_max_tokens)
        generation["response_format"] = {"type": "json_object"}
        if provider == "helmcode":
            generation["reasoning_effort"] = "none"
        return generation

    def _select_chat(self, provider, payload, questions, *, fallback=False):
        endpoint, model, token = self._chat_config(provider)
        generation = self._chat_generation(provider)
        body = {
            "model": model,
            **generation,
            "messages": [
                {
                    "role": "system",
                    "content": INSTRUCTIONS
                    + " Return JSON with an answers object mapping each requested field "
                    "to one exact candidate string or none.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "readers": payload["state"]["readers"],
                            "candidates": {
                                name: list(item["criteria"]) for name, item in questions.items()
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }

        def validate(data):
            answers = data.get("answers") if isinstance(data, dict) else None
            if not isinstance(answers, dict) or set(answers) != set(questions):
                raise ValueError("Invalid text selections")
            for name, value in answers.items():
                if not isinstance(value, str) or value not in questions[name]["criteria"]:
                    raise ValueError("Invented text candidate")
            return {
                "model": model,
                "usage": data.get("usage", {}),
                "answers": {
                    name: {"type": "choice", "choice": choice} for name, choice in answers.items()
                },
            }

        def call(mark_network_attempt):
            with self.http.session(self.settings.judge_timeout) as client:
                mark_network_attempt()
                response = client.post(
                    endpoint,
                    headers={"Authorization": "Bearer " + token} if token else {},
                    json=body,
                )
            record_response(provider, response.status_code, retry_after_s=retry_after(response))
            if not response.is_success:
                raise ProviderUnavailable(f"{provider} returned HTTP {response.status_code}")
            data = response.json()
            record_response(provider, response.status_code, data)
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete text response")
            content = choice["message"]["content"]
            if not isinstance(content, str) or len(content) > 20000:
                raise ValueError("Invalid text response")
            result = json.loads(content)
            validate(result)
            result["usage"] = data.get("usage", {})
            return result

        data = recorded_call(
            self.settings.data_dir / "provider-journal" / provider,
            {
                "endpoint": endpoint,
                "model": model,
                "instructions": INSTRUCTIONS,
                "payload": body["messages"][1]["content"],
                "generation": generation,
            },
            call,
            provider=provider,
            model=model,
            operation="text_selection",
            fallback=fallback,
            force=self.settings.ocr_force_recompute,
        )
        return validate(data)

    def _compatible(self, payload, questions):
        """The local/compatible judge can select existing candidates, never create facts."""
        body = {
            "model": self.settings.jev_model,
            **chat_generation(self.settings.jev_model, self.settings.judge_max_tokens),
            "messages": [
                {
                    "role": "system",
                    "content": INSTRUCTIONS
                    + ' Return JSON mapping each field to a candidate or "none".',
                },
                {"role": "user", "content": json.dumps(payload)},
            ],
        }
        endpoint = self.settings.judge_url.rstrip("/") + "/chat/completions"

        def call(mark_network_attempt):
            headers = {}
            if self.settings.judge_api_key:
                headers["Authorization"] = "Bearer " + self.settings.judge_api_key
            with self.http.session(self.settings.judge_timeout) as client:
                mark_network_attempt()
                response = client.post(endpoint, headers=headers, json=body)
            record_response("jev", response.status_code, retry_after_s=retry_after(response))
            response.raise_for_status()
            result = response.json()
            record_response("jev", response.status_code, result)
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Truncated or incomplete judge response")
            selected = json.loads(choice["message"]["content"])
            if set(selected) != set(questions) or any(
                value not in questions[name]["criteria"] for name, value in selected.items()
            ):
                raise ValueError("Judge must select existing candidates only")
            return {
                "answers": {
                    name: {"type": "choice", "choice": value} for name, value in selected.items()
                },
                "usage": result.get("usage", {}),
            }

        data = recorded_call(
            self.settings.data_dir / "provider-journal" / "jev",
            {"endpoint": endpoint, "payload": body},
            call,
            provider="jev",
            model=self.settings.jev_model,
            operation="text_selection",
            force=self.settings.ocr_force_recompute,
        )
        return {
            "role": "textual_recommendation_only",
            "visual_vote": False,
            "model": self.settings.jev_model,
            **data,
        }
