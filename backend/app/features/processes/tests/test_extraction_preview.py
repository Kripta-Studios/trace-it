"""Acceptance examples must not hide disabled document extraction."""

import pytest

from app.features.processes import draft_compilation
from app.features.processes.draft_schemas import DraftPlan
from app.features.processes.tests.test_drafts import plan


@pytest.mark.parametrize("source,expected_warning", [("none", True), ("document", False)])
async def test_preview_explains_required_external_inputs_without_rejecting_them(
    source, expected_warning
):
    data = plan()
    data["symbols"][0]["extraction"] = {"source": source, "labels": []}
    data["symbols"][0]["description"] = "Amount supplied by a reviewed external system."
    proposed = DraftPlan.model_validate(data)
    compiled = [
        {
            "text": "Amount above 100 requires review",
            "type": "prohibition",
            "decision": "REVIEW",
            "hash": "test",
            "code": "def evaluate(instance, sources, others):\n"
            "    return {'fires': instance['amount'] > 100, 'reason': 'LIMIT'}",
            "report": {"valid": True},
        }
    ]
    result = await draft_compilation.preview(None, None, proposed, {}, compiled)
    assert result["valid"] and all(example["passed"] for example in result["examples"])
    assert "not document ingestion or extraction" in result["examples_basis"]
    assert bool(result["extraction_warnings"]) is expected_warning
    if expected_warning:
        [warning] = result["extraction_warnings"]
        assert warning["symbol"] == "amount"
        assert warning["code"] == "REQUIRED_INPUT_EXTRACTION_DISABLED"
        assert "supplied externally" in warning["message"]
