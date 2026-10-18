"""Schema readings must be grounded in supplied OCR lines."""

import json
from dataclasses import replace

import httpx
import pytest

from app.features.ingestion.extraction_plan import ExtractionField
from app.features.ingestion.schema_fields import (
    SchemaFieldReader,
    normalize_schema_value,
    read_schema_fields,
)

from .conftest import lines


def field(name, kind="text", **kwargs):
    return ExtractionField(name=name, type=kind, **kwargs)


def test_labels_types_and_multiple_fields_on_one_line():
    requested = [
        field("fecha_caducidad", "date", labels=["Vence"]),
        field("peso", "number", labels=["Peso"]),
        field("aprobado", "boolean", labels=["Aprobado"]),
    ]
    result, warnings = read_schema_fields(
        lines("Vence: 21/04/2026  Peso: 1.234,50  Aprobado: Sí"), requested, 0.9
    )
    assert warnings == []
    assert {name: reading.value for name, reading in result.items()} == {
        "fecha_caducidad": "2026-04-21",
        "peso": "1234.50",
        "aprobado": "true",
    }
    result, _ = read_schema_fields(lines("Aprobado: No"), [field("aprobado", "boolean")], 0.9)
    assert result["aprobado"].value == "false"


def test_conflict_invalid_and_low_confidence_abstain():
    requested = [field("lote", labels=["Lote"]), field("peso", "number")]
    result, _ = read_schema_fields(lines("Lote: A\nLote: B\nPeso: 1,234"), requested, 0.9)
    assert result["lote"].value is None
    assert result["lote"].verification == "ambiguous"
    assert len(result["lote"].candidates) == 2
    assert result["peso"].value is None
    assert result["peso"].candidates[0].error == "Ambiguous separator"

    result, _ = read_schema_fields(lines("Lote: A", "ocr", 0.5), requested, 0.9)
    assert result["lote"].value is None
    assert result["lote"].proposed_value == "A"
    assert result["lote"].confidence is None


def test_overlapping_labels_do_not_capture_other_field():
    requested = [
        field("fecha", "date", labels=["Fecha"]),
        field("fecha_caducidad", "date", labels=["Fecha caducidad"]),
    ]
    result, _ = read_schema_fields(
        lines("Fecha caducidad: 22/04/2026 Fecha: 21/04/2026"), requested, 0.9
    )
    assert result["fecha_caducidad"].value == "2026-04-22"
    assert result["fecha"].value == "2026-04-21"


def test_negative_number_literal_name_and_unicode_offsets():
    requested = [
        field("company_name", labels=["Empresa"]),
        field("net_amount", "number"),
    ]
    result, _ = read_schema_fields(lines("Empresa: Straße GmbH net_amount: -12,50"), requested, 0.9)
    assert result["company_name"].value == "Straße GmbH"
    assert result["net_amount"].value == "-12.50"


def test_narrative_word_is_not_a_label():
    requested = [field("employee")]
    result, warnings = read_schema_fields(lines("Employee must sign"), requested, 0.9)
    assert warnings == []
    assert result["employee"].value is None
    assert result["employee"].candidates == []
    assert normalize_schema_value("-12,50", "number") == "-12.50"


def test_shared_label_abstains_for_all_fields_and_skips_model(settings, monkeypatch):
    requested = [
        field("holder", labels=["Name"]),
        field("applicant", labels=["Name"]),
    ]
    supplied = lines("Name: Ana Ruiz")
    result, warnings = read_schema_fields(supplied, requested, 0.9)
    assert warnings == [
        {
            "code": "SCHEMA_LABEL_COLLISION",
            "fields": ["applicant", "holder"],
            "locator": supplied[0].id,
        }
    ]
    for name in ("holder", "applicant"):
        assert result[name].value is None
        assert result[name].verification == "ambiguous"
        assert result[name].candidates[0].raw == "Ana Ruiz"
        assert result[name].candidates[0].error == "Ambiguous label shared by fields"

    settings = replace(settings, vlm_url="https://vision.example/v1", vlm_model="test-model")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    result, _ = SchemaFieldReader(settings).read(supplied, requested)
    assert all(reading.value is None for reading in result.values())


def test_unsupported_type_warns_without_extraction():
    result, warnings = read_schema_fields(lines("Objeto: abc"), [field("objeto", "object")], 0.9)
    assert result["objeto"].value is None
    assert warnings == [{"code": "SCHEMA_UNSUPPORTED_TYPE", "field": "objeto"}]


def test_model_selection_requires_exact_line_quote_and_journals(settings, monkeypatch):
    settings = replace(settings, vlm_url="https://vision.example/v1", vlm_model="test-model")
    requested = [field("fecha_caducidad", "date", labels=["Vencimiento"])]
    supplied = lines("Documento de prueba\nSe debe liquidar el 21/04/2026")
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "fecha_caducidad": {
                                        "line_id": supplied[1].id,
                                        "quote": "21/04/2026",
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    reader = SchemaFieldReader(settings)
    first, warnings = reader.read(supplied, requested)
    second, _ = reader.read(supplied, requested)
    assert warnings == []
    assert len(calls) == 1
    assert first["fecha_caducidad"].value == second["fecha_caducidad"].value == "2026-04-21"
    assert first["fecha_caducidad"].selected_by == "schema_model"
    assert first["fecha_caducidad"].verification == "extracted"
    assert first["fecha_caducidad"].candidates[0].raw == "21/04/2026"
    assert "fecha_caducidad" in calls[0]["messages"][1]["content"]


def test_model_invented_quote_is_discarded(settings, monkeypatch):
    settings = replace(settings, vlm_url="https://vision.example/v1", vlm_model="test-model")
    requested = [field("fecha_caducidad", "date")]
    supplied = lines("Sin fecha declarada")

    def respond(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "fecha_caducidad": {
                                        "line_id": supplied[0].id,
                                        "quote": "21/04/2026",
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    result, warnings = SchemaFieldReader(settings).read(supplied, requested)
    assert result["fecha_caducidad"].value is None
    assert result["fecha_caducidad"].candidates == []
    assert warnings == [{"code": "SCHEMA_READER_ERROR"}]


def test_model_may_omit_unknown_field(settings, monkeypatch):
    settings = replace(settings, vlm_url="https://vision.example/v1", vlm_model="test-model")

    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    result, warnings = SchemaFieldReader(settings).read(lines("Sin dato"), [field("lote")])
    assert result["lote"].value is None
    assert warnings == []


def test_gemini_selection_uses_same_quote_validation(settings, monkeypatch):
    settings = replace(settings, gemini_api_key="test-key")
    requested = [field("fecha_caducidad", "date")]
    supplied = lines("Vigencia hasta 21/04/2026")

    def respond(request):
        assert "x-goog-api-key" in request.headers
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "fecha_caducidad": {
                                                "line_id": supplied[0].id,
                                                "quote": "21/04/2026",
                                            }
                                        }
                                    )
                                }
                            ]
                        },
                    }
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    result, warnings = SchemaFieldReader(settings).read(supplied, requested)
    assert warnings == []
    assert result["fecha_caducidad"].value == "2026-04-21"
    assert result["fecha_caducidad"].selected_by == "schema_model"


def test_provider_failure_is_warning(settings, monkeypatch):
    settings = replace(settings, vlm_url="https://vision.example/v1", vlm_model="test-model")

    def fail(request):
        return httpx.Response(503, text="private data")

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(fail), **kwargs)
    )
    result, warnings = SchemaFieldReader(settings).read(lines("Unknown data"), [field("lote")])
    assert result["lote"].value is None
    assert warnings == [{"code": "SCHEMA_READER_ERROR"}]


def test_helm_schema_mapping_uses_pinned_vision_model_without_text_judge(settings):
    pinned = replace(
        settings,
        helmcode_api_key="offline",
        vision_providers=("helmcode",),
        helmcode_vision_models=("gemma4",),
        helmcode_text_model="qwen3.6",
        text_providers=(),
    )
    assert SchemaFieldReader(pinned)._chain() == [("helmcode", "gemma4")]
    with_text_judge = replace(pinned, text_providers=("helmcode",))
    assert SchemaFieldReader(with_text_judge)._chain() == [("helmcode", "qwen3.6")]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("120 EUR", "120"),
        ("120.01EUR", "120.01"),
        ("150 eur", "150"),
        ("€ 1.234,50", "1234.50"),
        ("USD1,234.50", "1234.50"),
        ("1 234,50 GBP", "1234.50"),
        ("£0", "0"),
        ("-12,50 $", "-12.50"),
        ("120.1234 EUR", "120.1234"),
    ],
)
def test_currency_annotated_schema_numbers_preserve_amount(raw, expected):
    assert normalize_schema_value(raw, "number") == expected
    result, warnings = read_schema_fields(
        lines(f"estimated_cost: {raw}\ncopies: 3"),
        [field("estimated_cost", "number"), field("copies", "number")],
        0.9,
    )
    assert warnings == []
    assert result["estimated_cost"].value == expected
    assert result["estimated_cost"].verification == "verified"
    assert result["estimated_cost"].candidates[0].raw == raw
    assert result["copies"].value == "3"


@pytest.mark.parametrize(
    "raw",
    [
        "1,234 EUR",
        "1.234 EUR",
        "12.34,50 EUR",
        "EUR120USD",
        "120 EUR USD",
        "1EUR20",
        "about 120 EUR",
        "120 EUR per copy",
        "120 kg",
        "EURNaN",
        "EUR",
        "120/150 EUR",
    ],
)
def test_currency_annotations_do_not_relax_ambiguous_or_non_numeric_values(raw):
    with pytest.raises(ValueError):
        normalize_schema_value(raw, "number")


def test_currency_parser_change_invalidates_schema_extraction_cache():
    from app.features.ingestion.extraction_plan import ExtractionPlan

    current = ExtractionPlan(process_id=1, fields=[field("estimated_cost", "number")])
    prior = current.model_copy(update={"version": "schema-v1"})
    assert current.version == "schema-v2"
    assert current.field_fingerprint != prior.field_fingerprint
    assert current.fingerprint != prior.fingerprint
