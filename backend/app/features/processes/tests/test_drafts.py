"""Documents -> conversation -> reviewed rules -> compilation -> backtest -> publication."""

import copy
import io
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select

from app.common.exceptions import ConflictError
from app.core.database import session_factory
from app.features.agents import llm
from app.features.decisions.model import Decision, Finding
from app.features.ingestion.model import File, Instance
from app.features.processes.draft_compilation import proposals, ready
from app.features.processes.draft_schemas import DraftPlan
from app.features.processes.model import Process
from app.features.rules.model import Rule
from app.main import app
from tests.support.models import per_role, user_json
from tests.support.users import manager


def scripted_pinned_models(monkeypatch):
    """Discovery now pins models; keep these workflow tests on the scripted role seam."""
    from dataclasses import replace

    original = llm.chain

    def chain(setup, role, failed):
        config = setup.settings.model_copy(update={"model": None, "fallback_models": []})
        return original(replace(setup, settings=config), role, failed)

    monkeypatch.setattr(llm, "chain", chain)


def plan(name=None, threshold=100, field="amount", default="PAY", review="REVIEW"):
    return {
        "name": name or f"discovery-{uuid.uuid4().hex[:10]}",
        "description": "Compare with Decimal(str(value)); missing required fields escalate.",
        "decision_types": [
            {"name": review, "priority": 2, "requires_human": True},
            {"name": default, "priority": 1, "is_default": True},
        ],
        "symbols": [{"name": field, "type": "number", "required": True}],
        "rules": [
            {
                "name": "limit",
                "text": f"{field} is greater than {threshold}.",
                "type": "prohibition",
                "decision": review,
                "evidence": [{"reference": "chat:1", "explanation": "Manager's threshold"}],
            }
        ],
        "examples": [
            {
                "name": "ordinary",
                "instance": {field: 20},
                "decision": default,
                "explanation": "Below the threshold",
            },
            {
                "name": "large",
                "instance": {field: 250},
                "decision": review,
                "explanation": "Above the threshold",
            },
            {
                "name": "missing",
                "instance": {},
                "decision": review,
                "explanation": "Required data missing",
            },
        ],
        "questions": [],
        "summary": "Proposed the threshold and its review examples.",
    }


def scripts(proposal, threshold=100, field="amount", broken=False):
    rule = proposal["rules"][0]
    normal = {
        "norm_rules": [
            {
                "number": 1,
                "text": f"{rule['text']} {rule['decision']}",
                "checks": [
                    {
                        "text": rule["text"],
                        "type": "prohibition",
                        "decision": rule["decision"],
                        "decision_source": "explicit",
                        "quote": rule["decision"],
                        "interpretation": "Escalate amounts above the confirmed threshold.",
                    }
                ],
            }
        ]
    }
    code = (
        "from decimal import Decimal\n"
        "def evaluate(instance, sources, others):\n"
        f"    fires = Decimal(str(instance['{field}'])) > {threshold}\n"
        "    return {'fires': fires, 'reason': 'LIMIT'}"
    )
    if broken:
        code = (
            "def evaluate(instance, sources, others):\n"
            "    return {'fires': False, 'reason': 'wrong'}"
        )
    tests = {
        "tests": [
            {
                "name": str(i),
                "instance_json": json.dumps({field: v}),
                "sources_json": "{}",
                "others_json": "[]",
                "fires": v > threshold,
            }
            for i, v in enumerate([0, 20, 40, 100, 200, 300])
        ]
    }
    return {
        "discovery": [proposal],
        "normalizer": [normal],
        "tester": [tests],
        "compiler": [{"code": code}] * (4 if broken else 1),
    }


@pytest.fixture
async def api(monkeypatch):
    scripted_pinned_models(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await manager(client, "Draft manager")
        yield client


async def post(api, draft, action, **body):
    if action == "messages":
        body.setdefault("mode", "revise")
    response = await api.post(
        f"/process-drafts/{draft['id']}/{action}", json={"revision": draft["revision"], **body}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def accept(api, draft):
    for key in proposals(DraftPlan.model_validate(draft["plan"])):
        draft = await post(api, draft, "reviews", proposal=key, disposition="accepted")
    return draft


async def prepare_new(api, monkeypatch, proposal=None):
    proposal = proposal or plan()
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal)))
    response = await api.post("/process-drafts", json={"name": proposal["name"]})
    assert response.status_code == 201, response.text
    draft = await post(api, response.json(), "messages", message="Review amounts above 100.")
    draft = await accept(api, draft)
    return await post(api, draft, "prepare")


async def test_new_process_compiles_without_activation_and_publishes_atomically(api, monkeypatch):
    draft = await prepare_new(api, monkeypatch)
    assert draft["preview"]["valid"]
    assert all(e["passed"] for e in draft["preview"]["examples"])
    async with session_factory() as session:
        assert not await session.scalar(
            select(Process.id).where(Process.name == draft["plan"]["name"])
        )
    published = await post(api, draft, "publish")
    pid = published["published_process_id"]
    rules = (await api.get(f"/processes/{pid}/rules")).json()
    assert len(rules) == 1 and rules[0]["status"] == "active"
    assert rules[0]["report"]["discovery"]["revision"] == draft["revision"]
    again = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": draft["revision"]}
    )
    assert again.status_code == 409
    history = (await api.get(f"/process-drafts/{draft['id']}/revisions")).json()
    assert history[0]["plan"]["rules"] == []
    assert len(history) == published["revision"]


async def add_decision(pid, author="engine"):
    async with session_factory() as session:
        digest = uuid.uuid4().hex
        session.add(File(hash=digest, name="invoice.pdf", content=b"invoice", text="invoice"))
        await session.flush()
        instance = Instance(
            process_id=pid,
            name="invoice.pdf",
            file_hash=digest,
            status="DECIDED",
            symbols={"amount": {"value": 50, "origin": "test"}},
        )
        session.add(instance)
        await session.flush()
        decision = Decision(
            instance_id=instance.id,
            decision="PAY",
            author=author,
            rules_hash="original",
            results=[],
            reason="original decision",
        )
        session.add(decision)
        await session.commit()
        return decision.id


@pytest.mark.parametrize("human", [False, True])
async def test_existing_process_backtest_keeps_identity_and_history(api, monkeypatch, human):
    initial = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = initial["published_process_id"]
    decision_id = await add_decision(pid, author="Manager" if human else "engine")
    start = (await api.post("/process-drafts", json={"process_id": pid})).json()
    proposal = plan(initial["plan"]["name"], threshold=40)
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal, threshold=40)))
    draft = await post(api, start, "messages", message="Lower the threshold to 40.")
    draft = await post(api, await accept(api, draft), "prepare")
    impact = draft["preview"]["impact"]
    # A person's decision never blocks publication (R01); it is reported, not rewritten.
    assert len(impact["resolved_by_person" if human else "changes"]) == 1
    assert not impact["conflicts"] and draft["preview"]["valid"]
    result = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": draft["revision"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["published_process_id"] == pid
    rules = (await api.get(f"/processes/{pid}/rules")).json()
    assert [r["status"] for r in rules] == ["retired", "active"]
    async with session_factory() as session:
        original = await session.get(Decision, decision_id)
        assert original.decision == "PAY" and original.rules_hash == "original"
        count = await session.scalar(
            select(func.count()).select_from(Finding).where(Finding.decision_id == decision_id)
        )
        assert count == (0 if human else 1)


async def test_stale_revision_rejection_and_chat_invalidate_preview(api, monkeypatch):
    draft = await prepare_new(api, monkeypatch)
    stale = copy.deepcopy(draft)
    draft = await post(
        api,
        draft,
        "reviews",
        proposal="rule:limit",
        disposition="rejected",
        explanation="Need to clarify the limit",
    )
    assert draft["preview"] is None
    r = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": stale["revision"]}
    )
    assert r.status_code == 409
    r = await api.post(
        f"/process-drafts/{draft['id']}/prepare", json={"revision": draft["revision"]}
    )
    assert r.status_code == 409 and "accept" in r.text
    revised = plan(draft["plan"]["name"], threshold=101)
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [revised]}))
    draft = await post(api, draft, "messages", message="Keep 101 after all.")
    assert draft["reviews"] == {} and draft["preview"] is None


async def test_manager_answer_resolves_question_but_does_not_approve_draft(api, monkeypatch):
    proposed = plan()
    proposed["questions"] = ["Should amounts above 100 escalate?"]
    proposed["rules"] = []
    proposed["examples"] = []
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [proposed]}))
    draft = (await api.post("/process-drafts", json={"name": proposed["name"]})).json()
    draft = await post(api, draft, "messages", message="Discover the amount policy.")
    draft = await accept(api, draft)
    response = await api.post(
        f"/process-drafts/{draft['id']}/prepare", json={"revision": draft["revision"]}
    )
    assert response.status_code == 409 and "questions" in response.text

    revised = plan(proposed["name"])
    reference = f"chat:{len(draft['messages']) + 1}"
    revised["rules"][0]["evidence"] = [
        {"reference": reference, "explanation": "Manager confirmed escalation above 100"}
    ]
    seen = {}
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [revised]}, seen))
    draft = await post(api, draft, "messages", message="Yes, escalate amounts above 100.")
    context = user_json(seen["discovery"][0])
    assert context["current_plan"]["questions"] == proposed["questions"]
    assert context["messages"][-1]["text"] == "Yes, escalate amounts above 100."
    assert draft["plan"]["questions"] == []
    assert draft["plan"]["rules"][0]["evidence"][0]["reference"] == reference
    assert draft["reviews"] == {} and draft["preview"] is None
    assert draft["published_process_id"] is None
    response = await api.post(
        f"/process-drafts/{draft['id']}/prepare", json={"revision": draft["revision"]}
    )
    assert response.status_code == 409 and "accept" in response.text


async def test_fixed_examples_reject_wrong_interpretation(api, monkeypatch):
    proposal = plan()
    proposal["examples"][0]["decision"] = "REVIEW"  # differs from the generated tests
    draft = await prepare_new(api, monkeypatch, proposal)
    assert not draft["preview"]["valid"]
    assert draft["preview"]["compilations"][0]["report"]["valid"]
    assert not draft["preview"]["examples"][0]["passed"]
    response = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": draft["revision"]}
    )
    assert response.status_code == 409


async def test_hiring_process_uses_same_workflow(api, monkeypatch):
    proposal = plan(field="assessment", default="HIRE", review="ESCALATE")
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal, field="assessment")))
    draft = (await api.post("/process-drafts", json={})).json()
    draft = await post(api, draft, "messages", message="Set up our supplied hiring rubric.")
    draft = await post(api, await accept(api, draft), "prepare")
    assert draft["preview"]["valid"]
    published = await post(api, draft, "publish")
    process = (await api.get(f"/processes/{published['published_process_id']}")).json()
    assert process["symbols"][0]["name"] == "assessment"


def workbook_bytes(formula=False):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Reference"
    sheet.append(["Vendor", "Limit"])
    sheet.append(["001", "=1+2" if formula else 100.01])
    sheet.append(["002", 200])
    policy = workbook.create_sheet("Policy")
    policy.append(["Ask the manager before accepting exceptions."])
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


async def test_workbook_mapping_preserves_cells_and_complete_rows(api, monkeypatch):
    draft = (await api.post("/process-drafts", json={})).json()
    r = await api.post(
        f"/process-drafts/{draft['id']}/workbooks",
        data={"revision": draft["revision"]},
        files={"file": ("reference.xlsx", workbook_bytes())},
    )
    assert r.status_code == 200, r.text
    draft = r.json()
    digest = draft["documents"][0]["document"]
    proposal = plan()
    proposal["sources"] = [
        {
            "name": "limits",
            "kind": "workbook",
            "document": digest,
            "sheet": "Reference",
            "first_row": 2,
            "last_row": 3,
            "columns": {"vendor": "A", "maximum": "B"},
            "explanation": "Limits",
            "evidence": [{"reference": f"{digest}:Reference!A1", "explanation": "Header"}],
        }
    ]
    seen = {}
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal), seen))
    draft = await post(
        api, draft, "messages", message="Use the reference table; escalate amounts above 100."
    )
    assert user_json(seen["discovery"][0])["workbooks"][0]["sheets"][1]["name"] == "Policy"
    draft = await post(api, await accept(api, draft), "prepare")
    assert draft["preview"]["source_counts"] == {"limits": 2}
    draft = await post(api, draft, "publish")
    source = (await api.get(f"/processes/{draft['published_process_id']}/sources/limits")).json()
    assert source["data"] == [
        {"vendor": "001", "maximum": "100.01"},
        {"vendor": "002", "maximum": "200"},
    ]


async def test_operator_cannot_create_or_review_drafts(api):
    user = await api.post(
        "/users",
        json={"name": "Operator", "role": "operator", "email": f"{uuid.uuid4().hex}@test.com"},
    )
    api.headers["X-User-Id"] = str(user.json()["id"])
    assert (await api.post("/process-drafts", json={})).status_code == 403


async def test_publication_refuses_new_history_since_preview(api, monkeypatch):
    original = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = original["published_process_id"]
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    proposal = plan(original["plan"]["name"])
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal)))
    draft = await post(api, draft, "messages", message="Keep the same policy.")
    draft = await post(api, await accept(api, draft), "prepare")
    await add_decision(pid)
    r = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": draft["revision"]}
    )
    assert r.status_code == 409 and "changed" in r.text
    async with session_factory() as session:
        assert len(list(await session.scalars(select(Rule).where(Rule.process_id == pid)))) == 1


async def test_failed_compilation_stays_a_draft(api, monkeypatch):
    proposal = plan()
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal, broken=True)))
    draft = (await api.post("/process-drafts", json={})).json()
    draft = await post(api, draft, "messages", message="Escalate amounts above 100.")
    draft = await post(api, await accept(api, draft), "prepare")
    assert not draft["preview"]["valid"]
    async with session_factory() as session:
        assert not await session.scalar(select(Process.id).where(Process.name == proposal["name"]))


async def test_erp_sync_is_complete_and_failed_sync_preserves_draft(api, monkeypatch):
    import httpx

    from app.features.processes import drafts
    from app.features.sources.http_connector import HttpConnector, SyncError
    from app.features.sources.tests.conftest import erp_config
    from app.features.sources.tests.test_http_connector import FAST, FakeERP

    config = erp_config("http://fake", **FAST)
    monkeypatch.setenv("TRACE_ERP_USER", "test")
    monkeypatch.setenv("TRACE_ERP_PASSWORD", "test")

    async def configured(*args):
        return {"erp": config}

    monkeypatch.setattr(drafts, "connectors", configured)
    connector = HttpConnector(config, httpx.MockTransport(FakeERP()))
    monkeypatch.setattr(drafts, "HttpConnector", lambda _: connector)
    draft = (await api.post("/process-drafts", json={})).json()
    draft = await post(api, draft, "sources/erp/sync")
    assert draft["snapshots"][0]["rows"] == 45

    async def broken():
        raise SyncError("page 2 unavailable")

    monkeypatch.setattr(connector, "download", broken)
    result = await api.post(
        f"/process-drafts/{draft['id']}/sources/erp/sync", json={"revision": draft["revision"]}
    )
    assert result.status_code == 502
    after = (await api.get(f"/process-drafts/{draft['id']}")).json()
    assert after["revision"] == draft["revision"]
    assert after["snapshots"] == draft["snapshots"]


async def test_chat_proposed_connector_requires_review_before_sync(api, monkeypatch):
    from app.features.processes import drafts
    from app.features.processes.tests.test_connector_proposals import connector_plan
    from app.features.sources import service as sources
    from app.features.sources.http_connector import Stats

    proposal = connector_plan().model_dump(mode="json")
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [proposal]}))
    draft = (await api.post("/process-drafts", json={"name": proposal["name"]})).json()
    draft = await post(
        api,
        draft,
        "messages",
        message="Connect the criminal-records registry described above.",
    )
    blocked = await api.post(
        f"/process-drafts/{draft['id']}/sources/criminal_records/sync",
        json={"revision": draft["revision"]},
    )
    assert blocked.status_code == 409

    registry_rows = [{"record_id": "CR-1", "full_name": "Ana Molina", "status": "ACTIVE"}]

    class Registry:
        base_url = "http://registry"
        stats = Stats(pages=1, requests=2, logins=1)

        def __init__(self, config):
            assert config.pagination.path == "/criminal/records"

        async def download(self):
            return registry_rows

    monkeypatch.setattr(drafts, "HttpConnector", Registry)
    draft = await post(
        api,
        draft,
        "reviews",
        proposal="connector:criminal_records",
        disposition="accepted",
    )
    draft = await post(api, draft, "sources/criminal_records/sync")
    assert draft["snapshots"] == [
        {
            "name": "criminal_records",
            "origin": f"discovery:criminal_records:{sources.rows_hash(registry_rows)}",
            "rows": 1,
        }
    ]


async def test_existing_setup_changes_require_review_and_publication(api, monkeypatch):
    original = await post(api, await prepare_new(api, monkeypatch), "publish")
    draft = (
        await api.post("/process-drafts", json={"process_id": original["published_process_id"]})
    ).json()
    changed = plan(original["plan"]["name"])
    changed["symbols"].append({"name": "new_field", "type": "text"})
    monkeypatch.setattr(llm, "model_for", per_role(scripts(changed)))
    draft = await post(api, draft, "messages", message="Add a field.")
    draft = await accept(api, draft)
    r = await api.post(
        f"/process-drafts/{draft['id']}/prepare", json={"revision": draft["revision"]}
    )
    assert r.status_code == 200 and r.json()["preview"]["valid"], r.text
    pid = original["published_process_id"]
    before = (await api.get(f"/processes/{pid}")).json()
    assert "new_field" not in {s["name"] for s in before["symbols"]}
    await post(api, r.json(), "publish")
    after = (await api.get(f"/processes/{pid}")).json()
    assert "new_field" in {s["name"] for s in after["symbols"]}


async def test_runtime_error_cannot_satisfy_an_escalation_example():
    from app.features.processes import draft_compilation

    proposed = DraftPlan.model_validate(plan())
    code = "def evaluate(instance, sources, others):\n    raise ValueError('broken rule')"
    compiled = [
        {
            "text": "test",
            "type": "prohibition",
            "decision": "REVIEW",
            "code": code,
            "hash": "test",
            "report": {"valid": True},
        }
    ]
    result = await draft_compilation.preview(None, None, proposed, {}, compiled)
    large = next(e for e in result["examples"] if e["name"] == "large")
    assert large["actual"] == "REVIEW"
    assert not large["passed"] and not result["valid"]


async def test_discovery_publishes_versions_and_captured_execution(api, monkeypatch):
    from app.features.versions.model import ProcessVersion

    initial = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = initial["published_process_id"]
    process = (await api.get(f"/processes/{pid}")).json()
    version_id = process["active_version_id"]
    assert version_id is not None
    decision_id = await add_decision(pid)
    async with session_factory() as session:
        decision = await session.get(Decision, decision_id)
        instance = await session.get(Instance, decision.instance_id)
        instance.status = "PENDING"
        await session.commit()
        iid = instance.id
    response = await api.post(f"/processes/{pid}/run")
    assert response.status_code == 200, response.text
    async with session_factory() as session:
        decision = await session.scalar(
            select(Decision).where(Decision.instance_id == iid).order_by(Decision.id.desc())
        )
        assert decision.version_id == version_id and decision.execution_id is not None
        replay_id = decision.id
        version = await session.get(ProcessVersion, version_id)
        assert version.validation["examples"]
    replay = await api.post(f"/decisions/{replay_id}/replay")
    assert replay.status_code == 200 and replay.json()["matches"]


@pytest.mark.parametrize("failure", ["runtime", "pending_review"])
async def test_discovery_preview_rejects_historical_errors_and_pending_reviews(
    api, monkeypatch, failure
):
    from app.features.decisions.model import DecisionReview

    initial = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = initial["published_process_id"]
    decision_id = await add_decision(pid)
    if failure == "pending_review":
        async with session_factory() as session:
            session.add(
                DecisionReview(
                    decision_id=decision_id,
                    status="completed",
                    recommendation="REVIEW",
                    reasoning="Needs investigation",
                    evidence=[],
                    requires_human=True,
                    snapshot={},
                )
            )
            await session.commit()
    proposal = plan(initial["plan"]["name"], threshold=40)
    responses = scripts(proposal, threshold=40)
    if failure == "runtime":
        code = responses["compiler"][0]["code"]
        responses["compiler"][0]["code"] = code.replace(
            "    fires =",
            "    if instance['amount'] == 50:\n"
            "        raise ValueError('historical failure')\n    fires =",
        )
    monkeypatch.setattr(llm, "model_for", per_role(responses))
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    draft = await post(api, draft, "messages", message="Lower the threshold to 40")
    draft = await post(api, await accept(api, draft), "prepare")
    assert not draft["preview"]["valid"]
    key = "errors" if failure == "runtime" else "conflicts"
    assert len(draft["preview"]["impact"][key]) == 1
    result = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": draft["revision"]}
    )
    assert result.status_code == 409


async def test_discovery_does_not_overwrite_a_process_draft(api, monkeypatch):
    initial = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = initial["published_process_id"]
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    proposal = plan(initial["plan"]["name"])
    monkeypatch.setattr(llm, "model_for", per_role(scripts(proposal)))
    draft = await post(api, draft, "messages", message="Keep the same policy")
    draft = await post(api, await accept(api, draft), "prepare")
    staged = await api.put(f"/processes/{pid}/draft", json={"description": "An unrelated edit"})
    assert staged.status_code == 200
    result = await api.post(
        f"/process-drafts/{draft['id']}/publish", json={"revision": draft["revision"]}
    )
    assert result.status_code == 409
    unchanged = (await api.get(f"/processes/{pid}/draft")).json()
    assert unchanged == staged.json()


async def test_a_revision_that_omits_the_name_keeps_the_one_the_manager_gave(api, monkeypatch):
    """Observed with the hiring pack: the third revision returned the whole plan with an
    empty name, and preparation then refused the draft for a reason the manager could not
    act on. The name is identity, not a proposal."""
    proposed = plan()
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [proposed]}))
    draft = (await api.post("/process-drafts", json={"name": proposed["name"]})).json()
    draft = await post(api, draft, "messages", message="Discover the amount policy.")
    assert draft["plan"]["name"] == proposed["name"]

    nameless = plan(proposed["name"]) | {"name": ""}
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [nameless]}))
    draft = await post(api, draft, "messages", message="Where my notes are silent, escalate.")
    assert draft["plan"]["name"] == proposed["name"]
    assert draft["plan"]["rules"], "the rest of the revision is kept as proposed"

    draft = await accept(api, draft)
    ready(DraftPlan.model_validate(draft["plan"]), draft["reviews"])  # no 409 about the name


async def test_a_revision_returns_the_trace_of_the_agent_run_it_performed(api, monkeypatch):
    """Observability: the caller gets a handle on what the agent just did, instead of
    scanning recent spans and guessing which ones were its own."""
    proposed = plan()
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [proposed]}))
    draft = (await api.post("/process-drafts", json={"name": proposed["name"]})).json()
    assert draft["trace_id"] is None
    draft = await post(api, draft, "messages", message="Discover the amount policy.")

    trace_id = draft["trace_id"]
    assert trace_id and len(trace_id) == 32
    assert draft["messages"][-1]["trace_id"] == trace_id
    spans = (await api.get(f"/traces/{trace_id}")).json()
    steps = {s["step"] for s in spans} | {c["step"] for s in spans for c in s["children"]}
    assert "discover_process" in steps and "llm_run" in steps
    assert (await api.get(f"/process-drafts/{draft['id']}")).json()["trace_id"] == trace_id


def workbook_plan():
    """A plan whose source renames a spreadsheet column, as discovery's own proposals do."""
    proposal = plan()
    proposal["sources"] = [
        {
            "name": "positions",
            "kind": "workbook",
            "explanation": "Open roles",
            "evidence": [{"reference": "abc:positions!A1", "explanation": "header"}],
            "document": "abc",
            "sheet": "positions",
            "first_row": 2,
            "last_row": 9,
            "columns": {"position_code": "A", "status": "D"},
        }
    ]
    return proposal


def test_an_example_keyed_by_the_spreadsheet_header_is_refused_before_compiling():
    """Observed with the hiring pack: the source mapped column A to `position_code`, the
    examples supplied rows keyed `code`, and because an example's rows replace the real
    table every rule failed in the sandbox after four minutes of compilation."""
    proposal = workbook_plan()
    proposal["examples"][0]["sources"] = {"positions": [{"code": "BE-1", "status": "open"}]}
    reviews = dict.fromkeys(proposals(DraftPlan.model_validate(proposal)), "accepted")
    with pytest.raises(ConflictError) as error:
        ready(DraftPlan.model_validate(proposal), reviews)
    assert "code" in str(error.value) and "position_code" in str(error.value)

    proposal["examples"][0]["sources"] = {"positions": [{"position_code": "BE-1"}]}
    ready(DraftPlan.model_validate(proposal), reviews)  # the mapped name is accepted


def test_an_example_inventing_a_table_is_refused():
    proposal = workbook_plan()
    proposal["examples"][0]["sources"] = {"applicants": [{"email": "a@b"}]}
    reviews = dict.fromkeys(proposals(DraftPlan.model_validate(proposal)), "accepted")
    with pytest.raises(ConflictError) as error:
        ready(DraftPlan.model_validate(proposal), reviews)
    assert "applicants" in str(error.value)


def symbol_plan(**extraction):
    proposal = plan()
    proposal["symbols"] = [
        {"name": "amount", "type": "number", "required": True, "extraction": extraction}
    ]
    return proposal


def test_a_symbol_that_can_only_read_the_whole_page_is_refused():
    """Observed with the hiring pack: all seven symbols were published with
    extraction.source "text", so each text field became the entire transcript, the typed
    ones became null, and all 44 cases escalated on data the reader had in front of it."""
    proposal = symbol_plan(source="text", labels=["Total"])
    reviews = dict.fromkeys(proposals(DraftPlan.model_validate(proposal)), "accepted")
    with pytest.raises(ConflictError) as error:
        ready(DraftPlan.model_validate(proposal), reviews)
    assert "whole transcript" in str(error.value) and "amount" in str(error.value)

    proposal = symbol_plan(source="document", labels=["Total"])
    ready(DraftPlan.model_validate(proposal), reviews)  # matching labels in the page is fine


def test_one_free_text_symbol_may_still_hold_the_transcript():
    """`text` exists for exactly that: a text symbol, with no labels to match."""
    proposal = plan()
    proposal["symbols"] = [
        {"name": "free_text", "type": "text", "extraction": {"source": "text"}},
        {"name": "amount", "type": "number", "required": True},
    ]
    reviews = dict.fromkeys(proposals(DraftPlan.model_validate(proposal)), "accepted")
    ready(DraftPlan.model_validate(proposal), reviews)


async def test_repeated_prepare_reuses_valid_compilation_and_runs_checks_again(api, monkeypatch):
    from app.features.processes import draft_compilation

    compile_calls, preview_calls = [], []
    compile_plan, preview = draft_compilation.compile_plan, draft_compilation.preview

    async def compile_once(*args, **kwargs):
        compile_calls.append(True)
        return await compile_plan(*args, **kwargs)

    async def recheck(*args, **kwargs):
        preview_calls.append(True)
        result = await preview(*args, **kwargs)
        # Reproduce valid artifacts blocked only by historical validation, not compilation.
        if len(preview_calls) == 1:
            result["valid"] = False
        return result

    monkeypatch.setattr(draft_compilation, "compile_plan", compile_once)
    monkeypatch.setattr(draft_compilation, "preview", recheck)
    draft = await prepare_new(api, monkeypatch)
    assert not draft["preview"]["valid"]
    repeated = await post(api, draft, "prepare")
    assert repeated["preview"]["valid"]
    assert len(compile_calls) == 1 and len(preview_calls) == 2
    assert repeated["preview"]["compilations"] == draft["preview"]["compilations"]


@pytest.mark.parametrize("change", ["plan", "execution", "source"])
async def test_prepare_cache_is_invalidated_by_candidate_edits(api, monkeypatch, change):
    from app.features.processes import draft_compilation

    compile_calls = []
    original = draft_compilation.compile_plan

    async def count_compilation(*args, **kwargs):
        compile_calls.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(draft_compilation, "compile_plan", count_compilation)
    draft = await prepare_new(api, monkeypatch)
    if change == "plan":
        revised = copy.deepcopy(draft["plan"])
        revised["description"] += " Preserve original currency."
        monkeypatch.setattr(llm, "model_for", per_role({"discovery": [revised]}))
        draft = await post(api, draft, "messages", message="Preserve original currency.")
    elif change == "execution":
        execution = copy.deepcopy(draft["execution"])
        execution["extraction"]["dpi"] = 300
        response = await api.put(
            f"/process-drafts/{draft['id']}/execution",
            json={"revision": draft["revision"], "execution": execution},
        )
        assert response.status_code == 200, response.text
        draft = response.json()
    else:
        response = await api.post(
            f"/process-drafts/{draft['id']}/workbooks",
            data={"revision": draft["revision"]},
            files={"file": ("updated-reference.xlsx", workbook_bytes())},
        )
        assert response.status_code == 200, response.text
        draft = response.json()
    assert draft["preview"] is None
    monkeypatch.setattr(llm, "model_for", per_role(scripts(draft["plan"])))
    repeated = await post(api, await accept(api, draft), "prepare")
    assert repeated["preview"]["valid"]
    assert len(compile_calls) == 2


@pytest.mark.parametrize("invalid_report", [{"valid": False}, None])
async def test_failed_artifacts_are_recompiled_on_repeated_prepare(
    api, monkeypatch, invalid_report
):
    from app.features.processes import draft_compilation

    calls = []
    original = draft_compilation.compile_plan
    original_preview = draft_compilation.preview

    async def count_compilation(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)

    async def failed_preview(*args, **kwargs):
        result = await original_preview(*args, **kwargs)
        if len(calls) == 1:
            result["compilations"][0]["report"] = invalid_report
            result["valid"] = False
        return result

    monkeypatch.setattr(draft_compilation, "compile_plan", count_compilation)
    monkeypatch.setattr(draft_compilation, "preview", failed_preview)
    draft = await prepare_new(api, monkeypatch)
    assert not draft["preview"]["valid"]
    monkeypatch.setattr(llm, "model_for", per_role(scripts(draft["plan"])))
    repeated = await post(api, draft, "prepare")
    assert repeated["preview"]["valid"] and len(calls) == 2
