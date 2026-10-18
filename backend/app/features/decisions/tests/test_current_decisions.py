"""Read current decisions without loading the append-only history into memory."""

from collections import Counter

from sqlalchemy import event, select

from app.core.database import session_factory
from app.features.decisions import service
from app.features.decisions.model import Decision
from app.features.decisions.tests.test_api import create_process
from app.features.rules.model import Rule
from tests.support.users import manager_client


async def test_latest_reads_exclude_history_and_summary_keeps_latest_engine_findings():
    async with manager_client() as api:
        process_id, _ = await create_process(api, "manager")

    async with session_factory() as session:
        instances = await service.instances_of(session, process_id)
        rule_id = await session.scalar(select(Rule.id).where(Rule.process_id == process_id))
        historical = []
        current_engine = []
        for instance in instances[:2]:
            instance.status = "DECIDED"
            for generation in range(3):
                decision = Decision(
                    instance_id=instance.id,
                    decision="PAGAR",
                    author="engine",
                    rules_hash="test",
                    results=[{"rule_id": rule_id, "fires": generation < 2}],
                )
                session.add(decision)
                await session.flush()
                (current_engine if generation == 2 else historical).append(decision.id)
        resolution = Decision(
            instance_id=instances[0].id,
            decision="NO_PAGAR",
            author="Manager",
            rules_hash="test",
            results=[],
            reason="Checked the evidence",
        )
        session.add(resolution)
        await session.commit()
        expected = {instances[0].id: resolution.id, instances[1].id: current_engine[1]}

    loaded = []

    def track_load(decision, context):
        loaded.append(decision.id)

    event.listen(Decision, "load", track_load)
    try:
        async with session_factory() as session:
            latest = await service.latest_decisions(session, instances)
            assert {i: d.id for i, d in latest.items()} == expected
            assert Counter(loaded) == Counter(expected.values())
            assert await service.latest_decisions(session, []) == {}

        loaded.clear()
        async with session_factory() as session:
            summary = await service.summary(session, process_id)
            assert summary.by_decision == {"NO_PAGAR": 1, "PAGAR": 1}
            assert summary.resolved == 1
            assert all(rule.fires == 0 for rule in summary.rules)
            assert summary.last_run_at is not None
            assert not set(loaded).intersection(historical)
            assert set(current_engine).issubset(loaded)
    finally:
        event.remove(Decision, "load", track_load)
