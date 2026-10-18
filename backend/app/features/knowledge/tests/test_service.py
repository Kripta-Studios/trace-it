"""Source discovery against PostgreSQL; fixtures are rolled back after every test."""

import os
import uuid

import pytest

from app.common.exceptions import NotFoundError
from app.core import events
from app.core.database import engine, session_factory
from app.features.ingestion.model import File, Instance
from app.features.knowledge.service import documents
from tests.support import rows

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRACEPAY_TEST_POSTGRES") != "1", reason="Requires test PostgreSQL"
    ),
]


@pytest.fixture
async def corpus():
    async with session_factory() as session:
        first = await rows.process(session, f"Knowledge {uuid.uuid4().hex}")
        other = await rows.process(session, f"Other {uuid.uuid4().hex}")

        async def add(name, text, process_id=None):
            source = File(hash=uuid.uuid4().hex, name=name, content=b"original", text=text)
            session.add(source)
            await session.flush()
            item = Instance(process_id=process_id or first.id, file_hash=source.hash, name=name)
            session.add(item)
            await session.flush()
            return item

        try:
            yield session, first, other, add
        finally:
            await session.rollback()
    await engine.dispose()


async def test_search_is_process_scoped_case_insensitive_and_returns_source_excerpt(corpus):
    session, first, other, add = corpus
    owned = await add("contract.pdf", "Supplier reference BLUE-42 has been approved.")
    await add("private.pdf", "BLUE-42 secret", other.id)
    result = await documents(session, first.id, "blue-42")
    assert [item.instance_id for item in result.items] == [owned.id]
    assert "BLUE-42" in result.items[0].excerpt
    assert result.items[0].has_text
    assert result.items[0].has_evidence is False
    assert result.next_cursor is None


async def test_sql_wildcards_are_literal(corpus):
    session, process, _, add = corpus
    wanted = await add("discount.pdf", "Discount is 10%_special / clause")
    await add("another.pdf", "Discount is 100Xspecial clause")
    result = await documents(session, process.id, "%_special")
    assert [item.instance_id for item in result.items] == [wanted.id]


async def test_cursor_pagination_has_no_duplicates_and_includes_unread_scans(corpus):
    session, process, _, add = corpus
    first = await add("scan.pdf", None)
    second = await add("notes.pdf", "Second")
    third = await add("letter.pdf", "Third")
    page = await documents(session, process.id, limit=2)
    assert [item.instance_id for item in page.items] == [third.id, second.id]
    assert page.next_cursor == second.id
    last = await documents(session, process.id, limit=2, cursor=page.next_cursor)
    assert [item.instance_id for item in last.items] == [first.id]
    assert last.items[0].has_text is False
    assert last.items[0].excerpt == ""
    assert last.next_cursor is None
    by_name = await documents(session, process.id, "SCAN")
    assert by_name.items[0].instance_id == first.id


async def test_latest_attached_evidence_wins_over_file_text_and_duplicate_upload(corpus):
    session, process, _, add = corpus
    item = await add("policy.pdf", "obsolete policy")
    for step, text, created in [
        ("ingest_document", "first reading", True),
        ("extract_document", "current policy REF-2026", None),
        ("ingest_document", "unattached duplicate", False),
    ]:
        events.record(
            session,
            step,
            process_id=process.id,
            instance_id=item.id,
            data={"created": created, "extraction": {"text": text}},
        )
        await session.flush()
    result = await documents(session, process.id, "REF-2026")
    assert len(result.items) == 1
    assert result.items[0].has_evidence
    assert result.items[0].excerpt == "current policy REF-2026"
    assert not (await documents(session, process.id, "obsolete")).items
    assert not (await documents(session, process.id, "unattached")).items


async def test_excerpt_is_bounded_and_centred_on_match(corpus):
    session, process, _, add = corpus
    await add("long.pdf", "a" * 3000 + " needle " + "z" * 4000)
    result = await documents(session, process.id, "needle")
    assert len(result.items[0].excerpt) <= 600
    assert "needle" in result.items[0].excerpt


async def test_missing_process_is_not_an_empty_library(corpus):
    session, _, _, _ = corpus
    with pytest.raises(NotFoundError):
        await documents(session, -1)
