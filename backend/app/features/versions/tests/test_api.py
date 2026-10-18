"""Publication and replay across real database transactions and sandbox executions."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.features.decisions.tests.test_api import client, stored
from app.features.ingestion.extraction_plan import load_extraction_plan
from app.features.ingestion.model import File, Instance
from app.features.processes.model import Process, Symbol
from app.features.rules.model import Rule
from app.features.rules.service import rule_hash
from app.features.sources.model import Source
from app.features.versions import configuration
from app.features.versions.model import ProcessDraft
from tests.support.users import anonymous

CODE = """def evaluate(instance, sources, others):
    duplicate = any(o.get("order") == instance.get("order") for o in others)
    paid = any(r["order"] == instance.get("order") for r in sources.get("paid", []))
    return {"fires": duplicate or paid, "reason": "DUPLICATE_OR_PAID" if duplicate or paid else ""}
"""
TYPES = [
    {"name": "PAY", "priority": 0, "is_default": True},
    {"name": "CHECK", "priority": 1, "requires_human": True},
    {"name": "REJECT", "priority": 2},
]


async def seed(api):
    suffix = uuid.uuid4().hex
    user = (
        await api.post(
            "/users", json={"name": "Manager", "email": f"{suffix}@x.test", "role": "manager"}
        )
    ).json()
    headers = {"X-User-Id": str(user["id"])}
    definition = {
        "name": suffix,
        "decision_types": TYPES,
        "symbols": [{"name": "order", "type": "text"}],
        "rules": [],
    }
    result = await api.post("/processes/definition", json=definition)
    assert result.status_code == 200, result.text
    pid = result.json()["process"]["id"]
    async with session_factory() as session:
        rule = Rule(
            process_id=pid,
            text="Check duplicate or paid orders",
            type="prohibition",
            decision="CHECK",
            code=CODE,
            hash=rule_hash("Check duplicate or paid orders", CODE),
            tests=[],
            report={"valid": True},
            status="draft",
        )
        session.add(rule)
        await session.flush()
        rid = rule.id
        for name, order in [("a", "A"), ("b", "B")]:
            digest = uuid.uuid4().hex
            session.add(File(hash=digest, name=name, content=b"pdf", text=""))
            await session.flush()
            session.add(
                Instance(
                    process_id=pid, file_hash=digest, name=name, symbols=stored({"order": order})
                )
            )
        session.add(Source(process_id=pid, name="paid", origin="test", rows=[{"order": "B"}]))
        await session.commit()
    draft = (await api.get(f"/processes/{pid}/draft", headers=headers)).json()
    result = await api.put(
        f"/processes/{pid}/draft",
        headers=headers,
        json={"expected_revision": draft["revision"], "rule_ids": [rid]},
    )
    assert result.status_code == 200, result.text
    return pid, headers, rid, definition


async def validate(api, pid, headers):
    result = await api.post(f"/processes/{pid}/draft/validate", headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


async def publish(api, pid, headers):
    draft = await validate(api, pid, headers)
    assert draft["validation"]["valid"], draft
    result = await api.post(
        f"/processes/{pid}/draft/publish",
        headers=headers,
        json={
            "revision": draft["revision"],
            "validation_hash": draft["validation"]["hash"],
            "reason": "Reviewed impact",
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


async def test_manual_source_schema_publishes_without_an_http_connector():
    async with client() as api:
        pid, headers, _, _ = await seed(api)
        schema = {"required": ["order"], "optional": [], "sync_before_run": False}
        async with session_factory() as session:
            draft = await session.get(ProcessDraft, pid)
            draft.snapshot = {**draft.snapshot, "source_schemas": {"paid": schema}}
            assert not draft.snapshot.get("connectors")
            await session.commit()
        published = await publish(api, pid, headers)
        assert published["snapshot"]["source_schemas"] == {"paid": schema}
        assert not published["snapshot"].get("connectors")
        result = await api.post(f"/processes/{pid}/run", headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["decided"] == 2


async def test_initial_approval_replay_and_rollback():
    async with client() as api:
        pid, headers, rid, _ = await seed(api)
        assert (await api.post(f"/processes/{pid}/run")).status_code == 409
        first = await publish(api, pid, headers)
        assert first["validation"]["valid"] and first["validation"]["inputs_hash"]
        assert (await api.post(f"/processes/{pid}/run")).json()["decided"] == 2
        cases = (await api.get(f"/processes/{pid}/instances")).json()
        original = (await api.get(f"/instances/{cases[0]['id']}")).json()["decisions"][0]
        assert original["version_id"] == first["id"]
        # New evidence and new rule configuration cannot change a historical replay.
        async with session_factory() as session:
            session.add(
                Source(process_id=pid, name="paid", origin="changed", rows=[{"order": "A"}])
            )
            row = await session.get(Rule, rid)
            row.code = (
                "def evaluate(instance, sources, others):\n"
                '    return {"fires": True, "reason": "changed"}'
            )
            await session.commit()
        result = await api.post(f"/decisions/{original['id']}/replay", headers=headers)
        assert result.json()["matches"], result.text
        # Stage retirement; old version remains in force until approval.
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"rule_ids": []})
        assert (await api.get(f"/processes/{pid}")).json()["active_version_id"] == first["id"]
        second = await publish(api, pid, headers)
        await api.put(
            f"/processes/{pid}/draft", headers=headers, json={"restore_version_id": first["id"]}
        )
        third = await publish(api, pid, headers)
        assert third["number"] == 3 and third["parent_id"] == second["id"]
        assert third["snapshot"] == first["snapshot"]
        assert (await api.get(f"/instances/{cases[0]['id']}")).json()["decisions"] == [original]


async def test_extraction_plan_uses_published_symbols_and_preserves_hints():
    async with client() as api:
        pid, headers, _, _ = await seed(api)
        await publish(api, pid, headers)
        async with session_factory() as session:
            symbol = await session.get(Symbol, (pid, "order"))
            symbol.extraction = {"labels": ["Purchase order"], "source": "document"}
            await session.commit()
            workspace = await configuration.workspace(session, pid)
            assert workspace["process"]["symbols"][0]["extraction"]["labels"] == ["Purchase order"]

        async with session_factory() as session:
            cached_process = await session.get(Process, pid)
            original = await load_extraction_plan(session, pid)
            assert original.fields[0].labels == []
            draft = await api.put(
                f"/processes/{pid}/draft",
                headers=headers,
                json={
                    "symbols": [
                        {
                            "name": "order",
                            "type": "text",
                            "extraction": {"labels": ["Purchase order"]},
                        },
                        {
                            "name": "expires_on",
                            "type": "date",
                            "extraction": {"labels": ["Valid until"]},
                        },
                    ]
                },
            )
            assert draft.status_code == 200, draft.text
            assert (await load_extraction_plan(session, pid)).fingerprint == original.fingerprint
            published = await publish(api, pid, headers)
            revised = await load_extraction_plan(session, pid)
            assert cached_process.active_version_id != published["id"]
            assert [field.name for field in revised.fields] == ["expires_on", "order"]
            assert revised.fields[0].labels == ["Valid until"]
            assert revised.fields[1].labels == ["Purchase order"]
            assert revised.field_fingerprint != original.field_fingerprint
            assert published["snapshot"]["process"]["symbols"][0]["extraction"]["labels"] == [
                "Purchase order"
            ]


async def test_new_required_symbol_reports_historical_coverage_without_waiving_rule_errors():
    async with client() as api:
        pid, headers, old_rule_id, _ = await seed(api)
        await publish(api, pid, headers)
        assert (await api.post(f"/processes/{pid}/run")).status_code == 200
        code = (
            "def evaluate(instance, sources, others):\n"
            "    return {'fires': instance['exposure_hours'] > 8, 'reason': 'EXPOSURE'}\n"
        )
        async with session_factory() as session:
            added = Rule(
                process_id=pid,
                text="Review long exposure",
                type="prohibition",
                decision="CHECK",
                code=code,
                hash=rule_hash("Review long exposure", code),
                tests=[],
                report={"valid": True},
                status="draft",
            )
            session.add(added)
            await session.commit()
            new_rule_id = added.id
        revised = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={
                "symbols": [
                    {"name": "order", "type": "text"},
                    {"name": "exposure_hours", "type": "number", "required": True},
                ],
                "rule_ids": [old_rule_id, new_rule_id],
            },
        )
        assert revised.status_code == 200, revised.text
        without_examples = await validate(api, pid, headers)
        assert not without_examples["validation"]["valid"]
        assert without_examples["validation"]["coverage"]["not_evaluable"] == 2
        examples = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={
                "expected_revision": without_examples["revision"],
                "acceptance_examples": [
                    {
                        "name": "present",
                        "instance": {"order": "A", "exposure_hours": 1},
                        "sources": {"paid": []},
                        "decision": "PAY",
                        "explanation": "Short exposure",
                    },
                    {
                        "name": "absent",
                        "instance": {"order": "A"},
                        "sources": {"paid": []},
                        "decision": "CHECK",
                        "explanation": "Missing required exposure",
                    },
                ],
            },
        )
        assert examples.status_code == 200, examples.text
        reviewed = await validate(api, pid, headers)
        report = reviewed["validation"]
        assert report["valid"] and not report["errors"] and not report["conflicts"]
        assert report["coverage"] == {
            "total": 2,
            "evaluated": 0,
            "not_evaluable": 2,
            "partial": 2,
            "none": 0,
        }
        assert len(report["not_evaluable"]) == 2
        assert all(row["evaluated_rules"] == [old_rule_id] for row in report["not_evaluable"])
        assert all(
            row["unavailable_rules"] == [{"rule_id": new_rule_id, "symbol": "exposure_hours"}]
            for row in report["not_evaluable"]
        )
        assert all(row["missing_symbols"] == ["exposure_hours"] for row in report["not_evaluable"])

        # The same incomplete cases cannot hide an unrelated error in an existing rule.
        failing = (
            "def evaluate(instance, sources, others):\n"
            "    result = 1 / 0\n"
            "    return {'fires': instance['exposure_hours'] > result, 'reason': 'BROKEN'}\n"
        )
        async with session_factory() as session:
            rule = await session.get(Rule, new_rule_id)
            rule.code = failing
            rule.hash = rule_hash(rule.text, failing)
            await session.commit()
        # Refresh the staged rule artifact to the deliberately broken implementation.
        refreshed = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={
                "expected_revision": reviewed["revision"],
                "rule_ids": [old_rule_id, new_rule_id],
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        blocked = await validate(api, pid, headers)
        assert not blocked["validation"]["valid"]
        assert len(blocked["validation"]["errors"]) == 2
        assert all("ZeroDivisionError" in row["reason"] for row in blocked["validation"]["errors"])


async def test_existing_required_data_and_undeclared_rule_access_remain_blocking():
    async with client() as api:
        pid, headers, rule_id, _ = await seed(api)
        await publish(api, pid, headers)
        assert (await api.post(f"/processes/{pid}/run")).status_code == 200
        async with session_factory() as session:
            case = await session.scalar(
                select(Instance).where(Instance.process_id == pid).order_by(Instance.id)
            )
            case.symbols = stored({})
            await session.commit()
        required = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={"symbols": [{"name": "order", "type": "text", "required": True}]},
        )
        assert required.status_code == 200, required.text
        reviewed = await validate(api, pid, headers)
        assert not reviewed["validation"]["valid"]
        assert any(
            "MISSING_EXISTING_REQUIRED: order" in row["reason"]
            for row in reviewed["validation"]["errors"]
        )

        code = (
            "def evaluate(instance, sources, others):\n"
            "    return {'fires': bool(instance['undeclared']), 'reason': 'UNKNOWN'}\n"
        )
        async with session_factory() as session:
            rule = await session.get(Rule, rule_id)
            rule.code = code
            rule.hash = rule_hash(rule.text, code)
            await session.commit()
        revised = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={"expected_revision": reviewed["revision"], "rule_ids": [rule_id]},
        )
        assert revised.status_code == 200, revised.text
        blocked = await validate(api, pid, headers)
        assert not blocked["validation"]["valid"]
        assert "unknown symbols" in blocked["validation"]["error"]


@pytest.mark.parametrize("missing_values", [{}, {"order": None}, {"order": "  "}])
async def test_cases_already_escalated_for_missing_required_data_do_not_block(missing_values):
    async with client() as api:
        pid, headers, rule_id, _ = await seed(api)
        cases = {}
        async with session_factory() as session:
            rule = await session.get(Rule, rule_id)
            rule.code = (
                "def evaluate(instance, sources, others):\n"
                "    if not instance.get('order'):\n"
                "        raise ValueError('MISSING_INPUT: order')\n"
                "    return {'fires': False, 'reason': 'OK'}\n"
            )
            rule.hash = rule_hash(rule.text, rule.code)
            for case in await session.scalars(select(Instance).where(Instance.process_id == pid)):
                cases[case.name] = case.id
                if case.name == "b":
                    case.symbols = stored(missing_values)
            await session.commit()
        draft = (await api.get(f"/processes/{pid}/draft", headers=headers)).json()
        required = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={
                "expected_revision": draft["revision"],
                "symbols": [{"name": "order", "type": "text", "required": True}],
                "rule_ids": [rule_id],
            },
        )
        assert required.status_code == 200, required.text
        await publish(api, pid, headers)
        assert (await api.post(f"/processes/{pid}/run")).status_code == 200
        [queued] = (await api.get(f"/processes/{pid}/queue")).json()
        assert queued["reason"] == "MISSING_DATA: order"

        # Friday's scans: escalated for the field they lack, so the policy already applied.
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"description": "v2"})
        report = (await validate(api, pid, headers))["validation"]
        assert report["valid"] and not report["errors"], report
        assert report["already_escalated"] == [
            {"instance_id": cases["b"], "name": "b", "reason": "MISSING_DATA: order"}
        ]
        assert report["coverage"] == {
            "total": 2,
            "evaluated": 1,
            "not_evaluable": 1,
            "partial": 0,
            "none": 1,
        }
        assert report["not_evaluable"][0]["missing_symbols"] == ["order"]
        assert report["not_evaluable"][0]["unavailable_rules"] == [
            {"rule_id": rule_id, "symbol": "order"}
        ]
        await publish(api, pid, headers)

        # A case decided PAY that lacks the field is still an error.
        async with session_factory() as session:
            (await session.get(Instance, cases["a"])).symbols = stored({})
            await session.commit()
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"description": "v3"})
        blocked = (await validate(api, pid, headers))["validation"]
        assert not blocked["valid"]
        assert blocked["errors"] == [
            {"instance_id": cases["a"], "reason": "MISSING_EXISTING_REQUIRED: order"}
        ]

        # Missing-input guards never excuse an unrelated defect reached before the read.
        async with session_factory() as session:
            code = "def evaluate(instance, sources, others):\n    return 1 / 0\n"
            broken_rule = Rule(
                process_id=pid,
                text="Broken independent check",
                type="prohibition",
                decision="CHECK",
                code=code,
                hash=rule_hash("Broken independent check", code),
                tests=[],
                report={"valid": True},
                status="draft",
            )
            session.add(broken_rule)
            await session.commit()
            broken_id = broken_rule.id
        current = (await api.get(f"/processes/{pid}/draft", headers=headers)).json()
        revised = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={"rule_ids": [broken_id], "expected_revision": current["revision"]},
        )
        assert revised.status_code == 200, revised.text
        broken = (await validate(api, pid, headers))["validation"]
        assert not broken["valid"]
        assert len(broken["errors"]) == 2
        assert all("ZeroDivisionError" in row["reason"] for row in broken["errors"])


async def test_stale_evidence_and_revision_refuse_publication():
    async with client() as api:
        pid, headers, _, _ = await seed(api)
        draft = await validate(api, pid, headers)
        body = {
            "revision": draft["revision"],
            "validation_hash": draft["validation"]["hash"],
            "reason": "Approved",
        }
        async with session_factory() as session:
            session.add(Source(process_id=pid, name="paid", origin="new", rows=[]))
            await session.commit()
        assert (
            await api.post(f"/processes/{pid}/draft/publish", headers=headers, json=body)
        ).status_code == 409
        changed = await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={"expected_revision": draft["revision"], "description": "Changed"},
        )
        assert changed.status_code == 200
        assert (
            await api.post(f"/processes/{pid}/draft/publish", headers=headers, json=body)
        ).status_code == 409
        await publish(api, pid, headers)


async def test_historical_queue_resolution_and_pack_reload():
    async with client() as api:
        pid, headers, _, definition = await seed(api)
        first = await publish(api, pid, headers)
        await api.post(f"/processes/{pid}/run")
        [case] = (await api.get(f"/processes/{pid}/queue")).json()
        definition["decision_types"] = [
            TYPES[0],
            {"name": "NEW_CHECK", "priority": 1, "requires_human": True},
        ]
        loaded = await api.post("/processes/definition", json=definition)
        assert loaded.status_code == 200, loaded.text
        assert (await api.get(f"/processes/{pid}")).json()["active_version_id"] == first["id"]
        await publish(api, pid, headers)
        assert (await api.get(f"/processes/{pid}/queue")).json()[0]["id"] == case["id"]
        response = await api.post(
            f"/instances/{case['id']}/resolve",
            headers=headers,
            json={"decision": "REJECT", "reason": "Reviewed original evidence"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["decisions"][-1]["version_id"] == first["id"]


async def test_invalid_rules_and_manager_gate():
    async with client() as api:
        pid, headers, rid, _ = await seed(api)
        assert (await anonymous("POST", f"/processes/{pid}/draft/validate")).status_code == 401
        async with session_factory() as session:
            rule = await session.get(Rule, rid)
            rule.report = {"valid": False}
            await session.commit()
        draft = (await api.get(f"/processes/{pid}/draft", headers=headers)).json()
        await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={"expected_revision": draft["revision"], "rule_ids": [rid]},
        )
        assert not (await validate(api, pid, headers))["validation"]["valid"]


async def test_concurrent_publish_once():
    async with client() as api:
        pid, headers, _, _ = await seed(api)
        draft = await validate(api, pid, headers)
        body = {
            "revision": draft["revision"],
            "validation_hash": draft["validation"]["hash"],
            "reason": "Reviewed",
        }
        replies = await asyncio.gather(
            *(
                api.post(f"/processes/{pid}/draft/publish", headers=headers, json=body)
                for _ in range(2)
            )
        )
        assert sorted(r.status_code for r in replies) == [201, 404]
        assert len((await api.get(f"/processes/{pid}/versions")).json()) == 1


async def test_validation_and_refused_publication_are_traced():
    """A validation is one span holding its replayed rules, never counted as execution; a
    refused publication is an error span."""
    async with client() as api:
        pid, headers, rid, _ = await seed(api)
        await publish(api, pid, headers)
        assert (await api.post(f"/processes/{pid}/run")).status_code == 200
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"description": "New"})
        draft = await validate(api, pid, headers)
        body = {"revision": draft["revision"], "validation_hash": "stale", "reason": "x"}
        refused = await api.post(f"/processes/{pid}/draft/publish", headers=headers, json=body)
        assert refused.status_code == 409
        params = {"process_id": pid, "name": "validate_process_draft"}
        check = (await api.get("/traces", params=params)).json()[0]
        tree = (await api.get(f"/traces/{check['trace_id']}")).json()
        params["name"] = "publish_process_version"
        published = (await api.get("/traces", params=params)).json()
        execution = (await api.get(f"/processes/{pid}/metrics/execution")).json()
    assert check["status"] == "ok" and check["data"]["author"] == "Manager"
    assert [check["data"][k] for k in ("changes", "conflicts", "errors")] == [0, 0, 0]
    [root] = [n for n in tree if n["step"] == "validate_process_draft"]
    assert [(c["step"], c["rule_id"], c["process_id"]) for c in root["children"]] == [
        ("evaluate_rule", rid, pid)
    ]
    assert published[0]["status"] == "error" and "validation" in published[0]["data"]["error"]
    # Only the run's evaluation: the validation replayed the rule too, and does not count.
    assert [(r["rule_id"], r["evaluations"]) for r in execution["rules"]] == [(rid, 1)]


async def test_publication_waits_for_a_batch_and_rejects_its_stale_preview(monkeypatch):
    import threading

    from app.features.agents import sandbox

    entered, release = threading.Event(), threading.Event()
    original_runner = sandbox.run_dataset

    def paused(*args):
        entered.set()
        assert release.wait(10), "test did not release execution"
        return original_runner(*args)

    async with client() as api:
        pid, headers, _, _ = await seed(api)
        first = await publish(api, pid, headers)
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"rule_ids": []})
        draft = await validate(api, pid, headers)
        monkeypatch.setattr(sandbox, "run_dataset", paused)
        running = asyncio.create_task(api.post(f"/processes/{pid}/run"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            approving = asyncio.create_task(
                api.post(
                    f"/processes/{pid}/draft/publish",
                    headers=headers,
                    json={
                        "revision": draft["revision"],
                        "validation_hash": draft["validation"]["hash"],
                        "reason": "Reviewed",
                    },
                )
            )
            await asyncio.sleep(0)
        finally:
            release.set()
        assert (await running).status_code == 200
        assert (await approving).status_code == 409
        for case in (await api.get(f"/processes/{pid}/instances")).json():
            decisions = (await api.get(f"/instances/{case['id']}")).json()["decisions"]
            assert {d["version_id"] for d in decisions} == {first["id"]}


async def test_resolved_escalation_does_not_block_publication_and_operator_cannot_approve():
    async with client() as api:
        pid, headers, rid, _ = await seed(api)
        await publish(api, pid, headers)
        await api.post(f"/processes/{pid}/run")
        [case] = (await api.get(f"/processes/{pid}/queue")).json()
        resolved = await api.post(
            f"/instances/{case['id']}/resolve",
            headers=headers,
            json={"decision": "PAY", "reason": "Confirmed not paid"},
        )
        assert resolved.status_code == 200, resolved.text

        async def stage(code):
            async with session_factory() as session:
                rule = Rule(
                    process_id=pid,
                    text="Reject paid orders",
                    type="prohibition",
                    decision="REJECT",
                    code=code,
                    hash=rule_hash("Reject paid orders", code),
                    tests=[],
                    report={"valid": True},
                    status="draft",
                )
                session.add(rule)
                await session.commit()
                return rule.id

        broken = await stage(
            "def evaluate(instance, sources, others):\n"
            "    return {'fires': 1 / 0 > 0, 'reason': 'PAID'}\n"
        )
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"rule_ids": [rid, broken]})
        blocked = await validate(api, pid, headers)
        assert not blocked["validation"]["valid"] and blocked["validation"]["errors"], blocked

        added = await stage(
            "def evaluate(instance, sources, others):\n"
            "    paid = any(r['order'] == instance.get('order') for r in sources.get('paid', []))\n"
            "    return {'fires': paid, 'reason': 'PAID' if paid else ''}\n"
        )
        await api.put(
            f"/processes/{pid}/draft",
            headers=headers,
            json={"expected_revision": blocked["revision"], "rule_ids": [rid, added]},
        )
        draft = await validate(api, pid, headers)
        report = draft["validation"]
        assert report["valid"] and not report["conflicts"] and not report["errors"], report
        [info] = report["resolved_by_person"]
        assert info["instance_id"] == case["id"]
        assert (info["before"], info["after"], info["resolution"]) == ("CHECK", "REJECT", "PAY")
        user = (
            await api.post(
                "/users",
                json={
                    "name": "Operator",
                    "email": f"{uuid.uuid4().hex}@x.test",
                    "role": "operator",
                },
            )
        ).json()
        response = await api.post(
            f"/processes/{pid}/draft/publish",
            headers={"X-User-Id": str(user["id"])},
            json={
                "revision": draft["revision"],
                "validation_hash": draft["validation"]["hash"],
                "reason": "Approve",
            },
        )
        assert response.status_code == 403
        await publish(api, pid, headers)
        latest = (await api.get(f"/instances/{case['id']}")).json()["decisions"][-1]
        assert (latest["decision"], latest["author"]) == ("PAY", "Manager")


async def test_new_duplicate_and_changed_symbols_do_not_change_replay():
    async with client() as api:
        pid, headers, _, _ = await seed(api)
        await publish(api, pid, headers)
        await api.post(f"/processes/{pid}/run")
        cases = (await api.get(f"/processes/{pid}/instances")).json()
        first = (await api.get(f"/instances/{cases[0]['id']}")).json()["decisions"][0]
        async with session_factory() as session:
            original = await session.get(Instance, cases[0]["id"])
            copy = Instance(
                process_id=pid,
                file_hash=original.file_hash,
                name="copy",
                symbols=stored({"order": "A"}),
            )
            session.add(copy)
            original.symbols = stored({"order": "B"})
            await session.commit()
        response = await api.post(f"/decisions/{first['id']}/replay", headers=headers)
        assert response.json()["matches"], response.text
        result = (await api.post(f"/processes/{pid}/reprocess?dry_run=true")).json()
        assert result["changes"][0]["before"] == "PAY"
        assert result["changes"][0]["after"] == "CHECK"


async def test_published_backtest_preserves_versions_and_decisions():
    async with client() as api:
        pid, headers, _, _ = await seed(api)
        first = await publish(api, pid, headers)
        await api.post(f"/processes/{pid}/run")
        cases = (await api.get(f"/processes/{pid}/instances")).json()
        before = [(await api.get(f"/instances/{case['id']}")).json() for case in cases]
        await api.put(f"/processes/{pid}/draft", headers=headers, json={"rule_ids": []})
        second = await publish(api, pid, headers)
        result = await api.post(f"/process-versions/{second['id']}/backtest", headers=headers)
        assert result.status_code == 200, result.text
        report = result.json()
        assert report["version_id"] == second["id"]
        assert report["coverage"]["total"] == 2
        assert report["unchanged"] == 1
        assert len(report["changes"] + report["conflicts"]) == 1
        after = [(await api.get(f"/instances/{case['id']}")).json() for case in cases]
        assert [c["decisions"] for c in after] == [c["decisions"] for c in before]
        assert (await api.get(f"/process-versions/{first['id']}")).json() == first
        assert (await api.get(f"/process-versions/{second['id']}")).json() == second
        assert (
            await api.post("/process-versions/999999999/backtest", headers=headers)
        ).status_code == 404
