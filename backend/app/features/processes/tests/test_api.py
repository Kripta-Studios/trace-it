"""The process and rule endpoints against the local database (`make test-db`)."""

import uuid
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.core.events import Event
from app.features.agents import llm
from app.features.decisions.model import Decision
from app.features.ingestion.model import File, Instance
from app.features.sources.model import Source
from app.features.versions.model import Execution, ProcessVersion
from tests.support.users import manager_client

INVOICES = {
    "decision_types": [
        {"name": "ESCALAR", "priority": 3, "requires_human": True},
        {"name": "NO_PAGAR", "priority": 2},
        {"name": "PAGAR", "priority": 1, "is_default": True},
    ],
    # `required` is optional: `supplier` leaves it out.
    "symbols": [
        {"name": "amount", "type": "number", "required": True},
        {"name": "supplier", "type": "text"},
    ],
}


@pytest.mark.parametrize("field", ["process", "symbol", "decision"])
@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
async def test_definition_rejects_blank_names_without_creating_process(field, name):
    definition = {"name": f"invalid-name-{uuid.uuid4().hex}", **deepcopy(INVOICES)}
    if field == "process":
        definition["name"] = name
    elif field == "symbol":
        definition["symbols"][0]["name"] = name
    else:
        definition["decision_types"][0]["name"] = name
    async with manager_client() as api:
        before = {row["id"] for row in (await api.get("/processes")).json()}
        response = await api.post("/processes/definition", json=definition)
        assert response.status_code == 422, response.text
        assert "must not be blank" in response.text
        after = {row["id"] for row in (await api.get("/processes")).json()}
        assert after == before


async def test_process_rule_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def llm_down(*args, **kwargs):
        raise llm.AgentError("no LLM in tests")

    monkeypatch.setattr(llm, "run", llm_down)
    suffix = uuid.uuid4().hex[:8]
    async with manager_client() as api:
        r = await api.post(
            "/users",
            json={"name": "Ana", "email": f"ana-{suffix}@x.com", "role": "manager"},
        )
        assert r.status_code == 201, r.text
        r = await api.post("/login", json={"email": f"ana-{suffix}@x.com"})
        assert r.status_code == 200, r.text
        headers = {"X-User-Id": str(r.json()["id"])}

        r = await api.post("/processes/definition", json={"name": f"invoices-{suffix}", **INVOICES})
        assert r.status_code == 200, r.text
        process = r.json()["process"]
        assert [t["name"] for t in process["decision_types"]] == ["ESCALAR", "NO_PAGAR", "PAGAR"]
        assert [t["requires_human"] for t in process["decision_types"]] == [True, False, False]
        assert [(s["name"], s["required"]) for s in process["symbols"]] == [
            ("amount", True),
            ("supplier", False),
        ]
        assert (await api.get(f"/processes/{process['id']}")).json() == process

        r = await api.post(
            f"/processes/{process['id']}/rules",
            json={
                "text": "If amount > 1000, escalate",
                "type": "prohibition",
                "decision": "ESCALAR",
            },
        )
        assert r.status_code == 201, r.text
        rule = r.json()
        # Saved as `compiling`; the background compilation fails (no LLM) and leaves it
        # a draft with the error; published configuration remains unchanged.
        assert rule["status"] == "compiling"
        rule = (await api.get(f"/rules/{rule['id']}")).json()
        assert rule["status"] == "draft"
        assert rule["report"]["valid"] is False
        assert rule["report"]["error"] == "CompilationError: no LLM in tests"

        r = await api.post(f"/rules/{rule['id']}/compile")
        assert r.status_code == 502, r.text
        assert r.json()["code"] == "compilation_failed"

        r = await api.post(f"/rules/{rule['id']}/activate", headers=headers)
        assert r.status_code == 409, r.text
        assert "not compiled" in r.json()["message"]


@pytest.mark.parametrize(
    ("types", "message"),
    [
        (
            [{"name": "REVIEW", "priority": 1, "is_default": True, "requires_human": True}],
            "default decision type cannot require a human",
        ),
        (
            [{"name": "PAGAR", "priority": 1, "is_default": True}],
            "At least one decision type must require a human",
        ),
        (
            [{"name": "A", "priority": 1, "is_default": True}, {"name": "B", "priority": 1}],
            "share a priority",
        ),
    ],
)
async def test_inconsistent_decision_types_are_refused(types: list, message: str) -> None:
    async with manager_client() as api:
        r = await api.post(
            "/processes/definition",
            json={"name": f"conflict-{uuid.uuid4().hex[:8]}", "decision_types": types},
        )
        assert r.status_code == 422, r.text
        assert message in r.text


async def test_manager_can_delete_an_unpublished_process() -> None:
    name = f"delete-me-{uuid.uuid4().hex[:8]}"
    async with manager_client() as api:
        response = await api.post(
            "/processes/definition",
            json={"name": name, **INVOICES},
        )
        assert response.status_code == 200, response.text
        process_id = response.json()["process"]["id"]

        response = await api.delete(f"/processes/{process_id}")

        assert response.status_code == 204, response.text
        assert (await api.get(f"/processes/{process_id}")).status_code == 404
        assert all(process["id"] != process_id for process in (await api.get("/processes")).json())


async def test_manager_can_delete_a_process_with_runtime_history() -> None:
    name = f"delete-history-{uuid.uuid4().hex[:8]}"
    async with manager_client() as api:
        response = await api.post(
            "/processes/definition",
            json={"name": name, **INVOICES},
        )
        assert response.status_code == 200, response.text
        process_id = response.json()["process"]["id"]

        digest = uuid.uuid4().hex
        async with session_factory() as session:
            session.add(File(hash=digest, name="history.pdf", content=b"pdf", text=""))
            session.add(Source(process_id=process_id, name="test", origin="test", rows=[]))
            await session.flush()
            instance = Instance(process_id=process_id, file_hash=digest, name="history.pdf")
            session.add(instance)
            await session.flush()

            from tests.support.rows import publish_fixture

            version = await publish_fixture(session, process_id)
            execution = Execution(process_id=process_id, version_id=version.id, inputs={})
            session.add(execution)
            await session.flush()
            decision = Decision(
                version_id=version.id,
                execution_id=execution.id,
                instance_id=instance.id,
                decision="PAGAR",
                results=[],
                rules_hash="test",
                author="engine",
            )
            session.add(decision)
            await session.flush()
            session.add(
                Event(
                    id=uuid.uuid4().int % 10**15,
                    trace_id="trace-delete",
                    span_id="span-delete",
                    step="test",
                    status="ok",
                    started_at=datetime.now(UTC),
                    process_id=process_id,
                    instance_id=instance.id,
                )
            )
            await session.commit()

        response = await api.delete(f"/processes/{process_id}")

        assert response.status_code == 204, response.text
        assert (await api.get(f"/processes/{process_id}")).status_code == 404

        async with session_factory() as session:
            assert (
                await session.scalar(select(Source.id).where(Source.process_id == process_id))
                is None
            )
            assert (
                await session.scalar(select(Instance.id).where(Instance.process_id == process_id))
                is None
            )
            assert (
                await session.scalar(
                    select(ProcessVersion.id).where(ProcessVersion.process_id == process_id)
                )
                is None
            )
            assert (
                await session.scalar(select(Event.id).where(Event.process_id == process_id)) is None
            )
            assert await session.get(File, digest) is None


async def test_manager_can_delete_all_process_owned_rows() -> None:
    name = f"delete-everything-{uuid.uuid4().hex[:8]}"
    async with manager_client() as api:
        response = await api.post(
            "/processes/definition",
            json={"name": name, **INVOICES},
        )
        assert response.status_code == 200, response.text
        process = response.json()["process"]
        process_id = process["id"]

        from app.features.alerts.model import Alert
        from app.features.decisions.model import DecisionReview, Finding
        from app.features.learning.model import Adoption, Analysis, Proposal, Validation
        from app.features.mail_ingestion.model import (
            MailAccount,
            MailActivity,
            MailActivityRead,
            MailAttachment,
            MailMessage,
            RunOperation,
        )
        from app.features.processes.model import DiscoveryRevision, DiscoverySession
        from app.features.proposals.model import ManagerProposal
        from app.features.rules.model import NormRule, Rule
        from app.features.users.model import User
        from app.features.versions.model import ProcessDraft

        digest = uuid.uuid4().hex
        async with session_factory() as session:
            session.add(File(hash=digest, name="everything.pdf", content=b"pdf", text=""))
            instance = Instance(process_id=process_id, file_hash=digest, name="everything.pdf")
            session.add_all(
                [Source(process_id=process_id, name="test", origin="test", rows=[]), instance]
            )
            await session.flush()

            from tests.support.rows import publish_fixture

            version = await publish_fixture(session, process_id)
            norm_rule = NormRule(process_id=process_id, number=1, text="The test rule")
            session.add(norm_rule)
            await session.flush()
            rule = Rule(
                process_id=process_id,
                norm_rule_id=norm_rule.id,
                text="The test rule",
                type="requirement",
                decision="PAGAR",
                status="draft",
            )
            session.add(rule)
            execution = Execution(process_id=process_id, version_id=version.id, inputs={})
            session.add(execution)
            await session.flush()
            decision = Decision(
                version_id=version.id,
                execution_id=execution.id,
                instance_id=instance.id,
                decision="PAGAR",
                results=[],
                rules_hash="test",
                author="engine",
            )
            session.add(decision)
            await session.flush()
            manager_id = await session.scalar(
                select(User.id).where(User.role == "manager").order_by(User.id)
            )
            assert manager_id is not None
            analysis = Analysis(
                process_id=process_id,
                author="test",
                snapshot={},
                reasoning="test",
            )
            session.add(analysis)
            await session.flush()
            proposal = Proposal(
                analysis_id=analysis.id,
                kind="deterministic",
                text="The test proposal",
                reasoning="test",
                evidence=[],
                counterexamples=[],
                limitations="test",
            )
            session.add(proposal)
            await session.flush()
            validation = Validation(
                proposal_id=proposal.id,
                author="test",
                baseline="test",
                snapshot={},
                report={},
            )
            session.add(validation)
            await session.flush()
            session.add(
                Adoption(
                    proposal_id=proposal.id,
                    validation_id=validation.id,
                    approved=True,
                    author="test",
                    reason="test",
                    snapshot={},
                )
            )
            session.add(
                DecisionReview(
                    decision_id=decision.id,
                    status="completed",
                    recommendation=None,
                    reasoning=None,
                    evidence=[],
                    requires_human=False,
                    snapshot={},
                    model=None,
                    error=None,
                )
            )
            session.add(Finding(decision_id=decision.id, rule_id=rule.id, type="test"))
            session.add(
                Alert(
                    process_id=process_id,
                    instance_id=instance.id,
                    decision_id=decision.id,
                    before="PAGAR",
                    after="PAGAR",
                    trigger={},
                    evidence={},
                    status="open",
                    acknowledged_by=None,
                    acknowledged_at=None,
                    note=None,
                )
            )
            session.add(
                ManagerProposal(
                    process_id=process_id,
                    instance_id=instance.id,
                    channel="chat",
                    kind="rule",
                    summary="test",
                    rationale="test",
                    evidence=[],
                    payload={},
                    status="open",
                    author="test",
                    resolved_by=None,
                    resolved_at=None,
                    outcome=None,
                )
            )
            account = MailAccount(
                process_id=process_id,
                host="imap.test",
                username="test",
                folder="INBOX",
                token_hash=uuid.uuid4().hex,
            )
            session.add(account)
            await session.flush()
            message = MailMessage(account_id=account.id, uidvalidity=1, uid=1)
            session.add(message)
            await session.flush()
            attachment = MailAttachment(
                message_id=message.id,
                part="1",
                original_name="everything.pdf",
                safe_name="everything.pdf",
                encoding="binary",
                advertised_size=3,
                file_hash=digest,
                instance_id=instance.id,
                execution_id=execution.id,
                decision_id=decision.id,
            )
            session.add(attachment)
            await session.flush()
            session.add_all(
                [
                    MailActivity(
                        process_id=process_id,
                        message_id=message.id,
                        attachment_id=attachment.id,
                        kind="received",
                        data={},
                    ),
                    MailActivityRead(process_id=process_id, user_id=manager_id, through_id=0),
                    RunOperation(
                        process_id=process_id,
                        key="test",
                        instance_ids=[instance.id],
                        result={},
                    ),
                ]
            )
            draft = ProcessDraft(
                process_id=process_id,
                base_version_id=version.id,
                revision=1,
                snapshot={},
                author="test",
            )
            session.add(draft)
            discovery = DiscoverySession(
                process_id=process_id,
                use_case_id=process["use_case_id"],
                published_process_id=process_id,
            )
            session.add(discovery)
            await session.flush()
            session.add(
                DiscoveryRevision(
                    draft_id=discovery.id,
                    number=1,
                    author_id=manager_id,
                    data={},
                )
            )
            await session.commit()

        response = await api.delete(f"/processes/{process_id}")

        assert response.status_code == 204, response.text
        assert (await api.get(f"/processes/{process_id}")).status_code == 404

        async with session_factory() as session:
            for _model, column in (
                (Source, Source.process_id),
                (Instance, Instance.process_id),
                (ProcessVersion, ProcessVersion.process_id),
                (Rule, Rule.process_id),
                (NormRule, NormRule.process_id),
                (Analysis, Analysis.process_id),
                (ManagerProposal, ManagerProposal.process_id),
                (Alert, Alert.process_id),
                (MailAccount, MailAccount.process_id),
                (MailActivity, MailActivity.process_id),
                (MailActivityRead, MailActivityRead.process_id),
                (RunOperation, RunOperation.process_id),
                (DiscoverySession, DiscoverySession.process_id),
            ):
                assert await session.scalar(select(column).where(column == process_id)) is None
            assert await session.get(File, digest) is None
