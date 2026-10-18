"""Draft validation must not bypass the names required by JSON import."""

import pytest

from app.features.versions.service import check_configuration


@pytest.mark.parametrize("field", ["name", "decision_types", "symbols"])
@pytest.mark.parametrize("name", ["", " \t\n"])
def test_unpublishable_blank_names_remain_readable(field, name):
    snapshot = {
        "process": {
            "id": 1,
            "name": "Expenses",
            "use_case_id": 1,
            "description": "",
            "decision_types": [
                {"name": "APPROVE", "priority": 1, "is_default": True},
                {"name": "REVIEW", "priority": 2, "requires_human": True},
            ],
            "symbols": [{"name": "amount", "type": "number"}],
        },
        "rules": [],
    }
    if field == "name":
        snapshot["process"][field] = name
    else:
        snapshot["process"][field][0]["name"] = name
    # Historical malformed records still deserialize for inspection and repair.
    from app.features.processes.schemas import ProcessDetail

    ProcessDetail.model_validate(snapshot["process"])
    with pytest.raises(ValueError, match="names? must not be blank"):
        check_configuration(snapshot)
