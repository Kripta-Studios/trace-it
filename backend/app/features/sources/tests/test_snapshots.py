"""Current source reads must stay bounded as append-only snapshot history grows."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from app.core.database import session_factory
from app.core.events import Event
from app.features.sources import service
from app.features.sources.model import Source
from tests.support import rows


async def test_current_loads_fetches_only_latest_snapshots_in_first_appearance_order():
    async with session_factory() as session:
        process = await rows.process(session, f"snapshots-{uuid.uuid4().hex}")
        other = await rows.process(session, f"snapshots-other-{uuid.uuid4().hex}")
        process_id = process.id
        for name, value in [("zeta", 1), ("alpha", 2), ("alpha", 3), ("zeta", 4)]:
            session.add(
                Source(process_id=process_id, name=name, origin="test", rows=[{"v": value}])
            )
        session.add(Source(process_id=other.id, name="zeta", origin="other", rows=[]))
        await session.commit()

    loaded = []

    def track_load(source, context):
        loaded.append(source.id)

    event.listen(Source, "load", track_load)
    try:
        async with session_factory() as session:
            current = await service.current_loads(session, process_id)
            assert [(s.name, s.rows) for s in current] == [
                ("zeta", [{"v": 4}]),
                ("alpha", [{"v": 3}]),
            ]
            assert loaded == [s.id for s in current]
            assert await service.current_loads(session, -1) == []
    finally:
        event.remove(Source, "load", track_load)


@pytest.mark.parametrize("status,error", [("error", "upstream unavailable"), ("ok", None)])
async def test_source_detail_preserves_latest_sync_status(status, error):
    checked_at = datetime.now(UTC)
    async with session_factory() as session:
        process = await rows.process(session, f"source-status-{uuid.uuid4().hex}")
        process_id = process.id
        session.add(Source(process_id=process_id, name="erp", origin="test", rows=[{"id": 1}]))
        session.add(
            Event(
                process_id=process_id,
                trace_id=uuid.uuid4().hex,
                span_id=uuid.uuid4().hex[:16],
                step="sync_source",
                status=status,
                started_at=checked_at,
                data={"source": "erp", **({"error": error} if error else {})},
            )
        )
        await session.commit()

    async with session_factory() as session:
        [listing] = await service.list_sources(session, process_id)
        detail = await service.get_source(session, process_id, "erp")
        assert detail.status == listing.status == ("down" if status == "error" else "ok")
        assert detail.error == listing.error == error
        assert detail.checked_at == listing.checked_at == checked_at
        assert detail.data == [{"id": 1}]
