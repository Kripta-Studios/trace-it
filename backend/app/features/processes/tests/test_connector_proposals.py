import pytest

from app.features.processes import draft_compilation
from app.features.processes.draft_schemas import DraftPlan
from app.features.versions import service as versions


def connector_plan() -> DraftPlan:
    return DraftPlan.model_validate(
        {
            "name": "Hiring screening",
            "description": "Decides whether an applicant advances, is rejected or needs review.",
            "decision_types": [
                {"name": "REVIEW", "priority": 2, "requires_human": True},
                {"name": "INTERVIEW", "priority": 1, "is_default": True},
            ],
            "connectors": [
                {
                    "name": "criminal_records",
                    "explanation": "Manager supplied the registry protocol.",
                    "evidence": [{"reference": "chat:1", "explanation": "Connection manual"}],
                    "required": ["record_id", "full_name", "status"],
                    "optional": ["conviction_date", "offense"],
                    "sync_before_run": True,
                    "config": {
                        "type": "http",
                        "base_url": {
                            "env": "TRACE_CRIMINAL_RECORDS_URL",
                            "default": "http://127.0.0.1:8010",
                        },
                        "format": {
                            "type": "xml",
                            "encoding": "iso-8859-1",
                            "error_code_path": "code",
                        },
                        "auth": {"type": "none"},
                        "pagination": {
                            "path": "/criminal/records",
                            "page_param": "page",
                            "page_size": 10,
                            "records_path": "records/record",
                            "total_path": "meta/total",
                            "pages_path": "meta/pages",
                        },
                        "key": "record_id",
                        "fields": {
                            "record_id": {"source": "id"},
                            "full_name": {"source": "name"},
                            "conviction_date": {"source": "conviction_date"},
                            "offense": {"source": "offense"},
                            "status": {"source": "status"},
                        },
                    },
                }
            ],
            "sources": [
                {
                    "name": "criminal_records",
                    "kind": "snapshot",
                    "snapshot": "criminal_records",
                    "explanation": "Registry snapshot to load after connector review",
                    "evidence": [
                        {"reference": "chat:1", "explanation": "Manager requested this source"}
                    ],
                }
            ],
        }
    )


def test_connector_is_a_reviewed_proposal_and_enters_the_version_snapshot():
    plan = connector_plan()
    assert "connector:criminal_records" in draft_compilation.proposals(plan)
    snapshot = draft_compilation.candidate(
        plan,
        [],
        {
            "process": {"id": 0, "use_case_id": 0},
            "rules": [],
            "agents": {},
            "guidance": {},
        },
    )
    assert snapshot["connectors"]["criminal_records"]["auth"] == {"type": "none"}
    assert snapshot["source_schemas"]["criminal_records"] == {
        "required": ["record_id", "full_name", "status"],
        "optional": ["conviction_date", "offense"],
        "sync_before_run": True,
    }
    versions.check_configuration(snapshot)


def test_connector_must_map_every_required_canonical_field():
    plan = connector_plan()
    plan.connectors[0].required.append("missing_field")
    snapshot = draft_compilation.candidate(
        plan,
        [],
        {
            "process": {"id": 0, "use_case_id": 0},
            "rules": [],
            "agents": {},
            "guidance": {},
        },
    )
    try:
        versions.check_configuration(snapshot)
    except ValueError as error:
        assert "missing_field" in str(error)
    else:
        raise AssertionError("unmapped required field was accepted")


def test_published_connector_without_a_schema_is_still_rejected():
    snapshot = draft_compilation.candidate(
        connector_plan(),
        [],
        {"process": {"id": 0, "use_case_id": 0}, "rules": [], "agents": {}, "guidance": {}},
    )
    snapshot["source_schemas"] = {}
    with pytest.raises(
        ValueError, match="Every published connector needs exactly one source schema"
    ):
        versions.check_configuration(snapshot)
