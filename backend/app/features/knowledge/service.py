"""Literal source discovery. No generation, new index, or document-byte retrieval."""

from sqlalchemy import func, or_, select, true

from app.core.events import Event
from app.features.ingestion.model import File, Instance
from app.features.ingestion.process_service import require_process
from app.features.knowledge.schemas import KnowledgeDocument, KnowledgePage


async def documents(session, process_id: int, query: str = "", limit: int = 25, cursor=None):
    await require_process(session, process_id)
    query = query.strip()
    # Match the reading selected by document_event: a duplicate upload must never replace
    # attached evidence, while an explicit re-extraction must become searchable immediately.
    # LATERAL reads only the latest text through events.instance_id's existing index.
    reading = (
        select(
            Event.id.label("event_id"),
            Event.data["extraction"]["text"].as_string().label("text"),
        )
        .where(
            Event.instance_id == Instance.id,
            Event.step.in_(("ingest_document", "extract_document")),
            or_(Event.step == "extract_document", Event.data["created"].as_boolean().is_not(False)),
        )
        .order_by(Event.id.desc())
        .limit(1)
        .lateral("reading")
    )
    text = func.coalesce(reading.c.text, File.text, "")
    start = func.greatest(func.strpos(func.lower(text), query.lower()) - 120, 1) if query else 1
    statement = (
        select(
            Instance.id.label("instance_id"),
            Instance.name,
            Instance.status,
            File.ingested_at.label("uploaded_at"),
            (func.length(func.trim(text)) > 0).label("has_text"),
            reading.c.event_id.is_not(None).label("has_evidence"),
            func.substr(text, start, 600).label("excerpt"),
        )
        .join(File, File.hash == Instance.file_hash)
        .outerjoin(reading, true())
        .where(Instance.process_id == process_id)
        .order_by(Instance.id.desc())
        .limit(limit + 1)
    )
    if query:
        statement = statement.where(
            or_(
                Instance.name.icontains(query, autoescape=True),
                text.icontains(query, autoescape=True),
            )
        )
    if cursor is not None:
        statement = statement.where(Instance.id < cursor)
    rows = (await session.execute(statement)).mappings().all()
    items = [KnowledgeDocument.model_validate(dict(row)) for row in rows[:limit]]
    return KnowledgePage(
        items=items,
        next_cursor=items[-1].instance_id if len(rows) > limit else None,
        query=query,
    )
