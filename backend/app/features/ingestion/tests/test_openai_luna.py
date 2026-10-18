"""Synthetic request contracts for Luna; these tests never call an external model."""

import json
from dataclasses import replace

import httpx
import pytest

from app.features.ingestion.config import Settings
from app.features.ingestion.ocr.errors import ProviderUnavailable
from app.features.ingestion.ocr.judge import TextJudge
from app.features.ingestion.ocr.vision import VisionFallback
from app.features.ingestion.pdf.committee import reconcile
from app.features.ingestion.schema_fields import SchemaFieldReader
from app.features.processes.execution import ExecutionSettings, ingestion_settings

from .conftest import lines
from .test_execution import config


@pytest.fixture
def luna(settings):
    return replace(
        settings,
        vlm_url="https://api.openai.com/v1",
        vlm_model="gpt-6-luna",
        vlm_api_key="synthetic-key",
        vision_providers=("compatible",),
        text_url="https://api.openai.com/v1",
        text_model="gpt-6-luna",
        text_api_key="synthetic-key",
        text_providers=("compatible",),
    )


def transport(monkeypatch, content, requests):
    original = httpx.Client

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert body["model"] == "gpt-6-luna"
        assert body["max_completion_tokens"] > 0
        assert body["reasoning_effort"] == "none"
        assert "max_tokens" not in body
        assert "temperature" not in body
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]},
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )


def test_luna_vision_uses_current_token_contract_and_real_reader_identity(luna, monkeypatch):
    requests = []
    transport(monkeypatch, "Lote: SYNTHETIC-12345", requests)
    reader = VisionFallback(luna)
    result = reader.transcribe(b"synthetic image", 1, (595, 842))
    assert result[0].raw == "Lote: SYNTHETIC-12345"
    assert "compatible:gpt-6-luna" in result[0].id
    assert reader.signature()["chain"][0]["generation"]["max_completion_tokens"] == 4096
    assert len(requests) == 1


@pytest.mark.parametrize("candidate", ["B98120774", "invented"])
def test_luna_text_judge_keeps_candidate_gate_and_journals_provider(luna, monkeypatch, candidate):
    requests = []
    transport(monkeypatch, json.dumps({"answers": {"supplier_tax_id": candidate}}), requests)
    readers = {"primary": lines("NIF: B98120774", "ocr", 0.99)}
    fields, _, _ = reconcile(readers, 0.9)
    judge = TextJudge(luna)
    if candidate == "invented":
        with pytest.raises(ProviderUnavailable):
            judge.select(readers, fields)
    else:
        result = judge.select(readers, fields)
        assert result["model"] == "gpt-6-luna"
        assert result["answers"]["supplier_tax_id"]["choice"] == candidate
        assert result["visual_vote"] is False
        assert judge.select(readers, fields) == result
        assert len(requests) == 1
    signature = judge.signature()
    assert signature["chain"][0]["provider"] == "compatible"
    assert "helmcode" not in json.dumps(signature)


def test_luna_schema_mapping_uses_same_generation_contract(luna, monkeypatch):
    requests = []
    transport(monkeypatch, '{"fields": {}}', requests)
    mapper = SchemaFieldReader(luna)
    result = mapper._select_provider("compatible", "gpt-6-luna", "{}", {}, [], lambda data: data)
    assert result == {"fields": {}}
    signature = mapper.signature()
    assert signature["chain"][0]["generation"]["max_completion_tokens"] == 1500
    assert len(requests) == 1


def test_versioned_compatible_judge_keeps_explicit_model_endpoint_and_credentials(
    luna, monkeypatch
):
    monkeypatch.setenv("TRACEPAY_VLM_URL", luna.vlm_url)
    value = config(luna).model_dump()
    value["local_only"] = False
    value["compatible_endpoint"] = luna.vlm_url
    value["extraction"].update(
        vision_model="compatible:gpt-6-luna", text_judge_model="compatible:gpt-6-luna"
    )
    bound = ingestion_settings(ExecutionSettings.model_validate(value), luna)
    assert bound.text_providers == ("compatible",)
    assert (bound.text_url, bound.text_model, bound.text_api_key) == (
        luna.text_url,
        "gpt-6-luna",
        "synthetic-key",
    )
    assert bound.judge_url is None
    assert TextJudge(bound).signature()["chain"][0]["provider"] == "compatible"
    assert bound.helmcode_api_key is None
    assert bound.jev_api_key is None


def test_text_configuration_reads_environment_without_exposing_key(monkeypatch):
    monkeypatch.setenv("TRACEPAY_TEXT_PROVIDERS", "compatible")
    monkeypatch.setenv("TRACEPAY_TEXT_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("TRACEPAY_TEXT_MODEL", "gpt-6-luna")
    monkeypatch.setenv("TRACEPAY_TEXT_API_KEY", "synthetic-private-key")
    settings = Settings()
    assert settings.summary()["text"] == [{"provider": "compatible", "model": "gpt-6-luna"}]
    assert "synthetic-private-key" not in repr(settings)
    assert "synthetic-private-key" not in json.dumps(settings.summary())
