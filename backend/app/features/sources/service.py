"""Sync an HTTP source into a new snapshot, and compare snapshots.

A sync downloads everything first and writes one `Source` row only if the download is
complete and its rows have the pack's canonical schema (`<pack>/schema.json`, ADR 0028); a
failed sync writes nothing and records why. Sources the schema marks `sync_before_run` are
synced at the start of every run; one that fails is down for that run, and no rule that
reads it runs on an older snapshot.
"""

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import NotFoundError, TraceError
from app.core import events
from app.core.config import settings
from app.core.events import Event
from app.features.processes.model import Process
from app.features.sources.http_connector import (
    HttpConnector,
    HttpSourceConfig,
    SourcesFile,
    SyncError,
)
from app.features.sources.model import Source
from app.features.use_cases.model import UseCase


class SourceUnavailableError(TraceError):
    status_code = 502
    code = "source_unavailable"


class SourceSchema(BaseModel):
    """The canonical shape rules see for one source, whatever system it comes from. A
    connector maps its own fields to these names (`fields` in `sources.json`)."""

    required: list[str]  # present and not blank in every row
    optional: list[str] = []
    sync_before_run: bool = False  # a live source: synced at the start of every run


class SchemaFile(BaseModel):
    sources: dict[str, SourceSchema]


class Diff(BaseModel):
    previous_id: int | None
    current_id: int | None
    added: list[str] = []
    removed: list[str] = []
    changed: dict[str, dict[str, list[Any]]] = {}  # key -> field -> [before, after]

    def summary(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
        }


class SyncResult(BaseModel):
    source_id: int
    origin: str
    rows: int
    rows_hash: str
    stats: dict[str, Any]
    diff: Diff


class SourceOut(BaseModel):
    id: int
    name: str
    origin: str
    rows: int
    loaded_at: datetime
    # From the latest sync attempt: "down" when it failed (no run reads this load until a
    # sync succeeds), "ok" when it worked, None for a source never synced (a workbook).
    status: str | None = None
    error: str | None = None
    checked_at: datetime | None = None


class SourceDetail(SourceOut):
    data: list[dict[str, Any]]  # the rows of this load, as the rules see them


async def current_loads(session: AsyncSession, process_id: int) -> list[Source]:
    """The latest load of each source, in order of first appearance. Every load is kept;
    only the last one per name is current."""
    latest = (
        select(func.max(Source.id).label("id"), func.min(Source.id).label("first_id"))
        .where(Source.process_id == process_id)
        .group_by(Source.name)
        .subquery()
    )
    # Do not transfer or deserialize the JSON of superseded snapshots on every read/run.
    return list(
        await session.scalars(
            select(Source).join(latest, Source.id == latest.c.id).order_by(latest.c.first_id)
        )
    )


def _out(load: Source, event: Event | None = None) -> SourceOut:
    return SourceOut(
        id=load.id,
        name=load.name,
        origin=load.origin,
        rows=len(load.rows),
        loaded_at=load.loaded_at,
        status=("down" if event.status == "error" else "ok") if event else None,
        error=(event.data or {}).get("error") if event else None,
        checked_at=event.started_at if event else None,
    )


async def sync_status(
    session: AsyncSession, process_id: int | None = None, since: datetime | None = None
) -> dict[tuple[int, str], Event]:
    """The latest `sync_source` span per process and source (ADR 0028)."""
    # ponytail: read from the audit spans, no status table; a table if spans are pruned.
    name = Event.data["source"].astext
    query = (
        select(Event)
        .where(Event.step == "sync_source")
        .distinct(Event.process_id, name)
        .order_by(Event.process_id, name, Event.id.desc())
    )
    if process_id is not None:
        query = query.where(Event.process_id == process_id)
    if since is not None:
        query = query.where(Event.started_at >= since)
    return {(e.process_id, e.data["source"]): e for e in await session.scalars(query)}


async def down_sources(session: AsyncSession, since: datetime | None = None) -> list[str]:
    """`<process id>:<source>` of every source whose latest sync failed."""
    return sorted(
        f"{p}:{name}"
        for (p, name), e in (await sync_status(session, since=since)).items()
        if e.status == "error"
    )


async def list_sources(session: AsyncSession, process_id: int) -> list[SourceOut]:
    if await session.get(Process, process_id) is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    status = await sync_status(session, process_id)
    out = []
    for load in await current_loads(session, process_id):
        out.append(_out(load, status.get((process_id, load.name))))
    return out


async def get_source(session: AsyncSession, process_id: int, name: str) -> SourceDetail:
    """The current load of one source, rows included."""
    if await session.get(Process, process_id) is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    load = await session.scalar(
        select(Source)
        .where(Source.process_id == process_id, Source.name == name)
        .order_by(Source.id.desc())
        .limit(1)
    )
    if load is None:
        raise NotFoundError(f"Process {process_id} has no load of source {name!r}")
    status = await sync_status(session, process_id)
    return SourceDetail(**_out(load, status.get((process_id, name))).model_dump(), data=load.rows)


def pack_sources_file(pack_file: Path) -> Path:
    """`processes/invoice-payment.json` keeps its sources in `processes/invoice-payment/`."""
    return pack_file.with_suffix("") / "sources.json"


def load_schema(pack_file: Path) -> dict[str, SourceSchema]:
    """The pack's canonical source schema. No file: nothing to validate, nothing synced
    before a run (an offline pack)."""
    path = pack_file.with_suffix("") / "schema.json"
    if not path.is_file():
        return {}
    return SchemaFile.model_validate_json(path.read_text(encoding="utf-8")).sources


async def process_pack(session: AsyncSession, process_id: int) -> Path | None:
    """The pack of the process's use case, if any."""
    process = await session.get(Process, process_id)
    if process is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    use_case = await session.get(UseCase, process.use_case_id)
    try:
        return find_pack(use_case.name)
    except NotFoundError:
        return None


def nonconforming(rows: list[dict[str, Any]], schema: SourceSchema, key: str) -> str | None:
    """Why `rows` are not in the canonical schema, or None. A required field absent, None
    or blank in any row fails the whole load: a rule would read it as "no data"."""
    bad: dict[str, list[str]] = {}
    for row in rows:
        for field in schema.required:
            if row.get(field) is None or not str(row[field]).strip():
                bad.setdefault(field, []).append(str(row.get(key)))
    return (
        "; ".join(
            f"{field} missing in {len(keys)} rows ({', '.join(keys[:5])})"
            for field, keys in sorted(bad.items())
        )
        or None
    )


def find_pack(use_case: str) -> Path:
    """The pack of a use case: the one whose `use_case` names it (or, without `use_case`,
    whose own `name` gave the use case its name). Its connectors serve every process of the
    use case (ADR 0013)."""
    for pack in sorted(settings.processes_dir.glob("*.json")):
        try:
            data = json.loads(pack.read_text(encoding="utf-8"))
            if (data.get("use_case") or data.get("name")) == use_case:
                return pack
        except (json.JSONDecodeError, AttributeError):
            continue
    raise NotFoundError(
        f"No process pack in {settings.processes_dir} is for the use case {use_case!r}"
    )


async def process_config(session: AsyncSession, process_id: int, name: str) -> HttpSourceConfig:
    """The connector of source `name` for a process: its use case's `sources.json`."""
    process = await session.get(Process, process_id)
    if process is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    if process.active_version_id:
        from app.features.versions.model import ProcessVersion

        version = await session.get(ProcessVersion, process.active_version_id)
        if row := (version.snapshot.get("connectors", {}) if version else {}).get(name):
            return HttpSourceConfig.model_validate(row)
    use_case = await session.get(UseCase, process.use_case_id)
    return load_config(pack_sources_file(find_pack(use_case.name)), name)


async def process_schemas(session: AsyncSession, process_id: int) -> dict[str, SourceSchema]:
    """Merge pack schemas with published schemas; the published entry wins by name."""
    process = await session.get(Process, process_id)
    if process is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    pack = await process_pack(session, process_id)
    schemas = load_schema(pack) if pack else {}
    if process.active_version_id:
        from app.features.versions.model import ProcessVersion

        version = await session.get(ProcessVersion, process.active_version_id)
        rows = version.snapshot.get("source_schemas", {}) if version else {}
        schemas.update({name: SourceSchema.model_validate(row) for name, row in rows.items()})
    return schemas


def load_config(sources_file: Path, source_name: str) -> HttpSourceConfig:
    """Read on every sync, so a changed limit or timeout applies without a restart."""
    if not sources_file.is_file():
        raise NotFoundError(f"{sources_file} does not exist")
    try:
        sources = SourcesFile.model_validate_json(sources_file.read_text(encoding="utf-8"))
    except ValidationError as e:
        raise SourceUnavailableError(f"{sources_file}: invalid configuration\n{e}") from e
    if source_name not in sources.sources:
        raise NotFoundError(f"{sources_file} does not declare the source {source_name!r}")
    return sources.sources[source_name]


def rows_hash(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def diff_rows(
    before: list[dict[str, Any]], after: list[dict[str, Any]], key: str
) -> tuple[list[str], list[str], dict[str, dict[str, list[Any]]]]:
    old = {str(r.get(key)): r for r in before}
    new = {str(r.get(key)): r for r in after}
    changed = {}
    for k in sorted(old.keys() & new.keys()):
        fields = {
            f: [old[k].get(f), new[k].get(f)]
            for f in sorted(old[k].keys() | new[k].keys())
            if old[k].get(f) != new[k].get(f)
        }
        if fields:
            changed[k] = fields
    return sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys()), changed


async def diff_latest(session: AsyncSession, process_id: int, name: str, key: str) -> Diff:
    """The latest snapshot of a source against the one before it."""
    last_two = list(
        await session.scalars(
            select(Source)
            .where(Source.process_id == process_id, Source.name == name)
            .order_by(Source.id.desc())
            .limit(2)
        )
    )
    current = last_two[0] if last_two else None
    previous = last_two[1] if len(last_two) > 1 else None
    added, removed, changed = diff_rows(
        previous.rows if previous else [], current.rows if current else [], key
    )
    return Diff(
        previous_id=previous.id if previous else None,
        current_id=current.id if current else None,
        added=added,
        removed=removed,
        changed=changed,
    )


async def sync(
    session: AsyncSession,
    process_id: int,
    name: str,
    config: HttpSourceConfig,
    transport: httpx.AsyncBaseTransport | None = None,
    schema: SourceSchema | None = None,
) -> SyncResult:
    if await session.get(Process, process_id) is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    started = datetime.now(UTC)
    # The connector's stats (requests, retries, 429s, logins, pages) go on the span.
    with events.span("sync_source", process_id=process_id, source=name) as span:
        failed = f"Sync of {name!r} failed; the previous snapshot stays stored, unused by runs"
        if schema and (unmapped := sorted(set(schema.required) - config.fields.keys())):
            raise SourceUnavailableError(f"{failed}. No field mapped to canonical {unmapped}")
        connector = None
        try:
            connector = HttpConnector(config, transport)
            rows = await connector.download()
        except SyncError as e:
            if connector:
                span.set(**connector.stats.__dict__)
            raise SourceUnavailableError(f"{failed}. {e}") from e
        if schema and (problem := nonconforming(rows, schema, config.key)):
            span.set(**connector.stats.__dict__)
            raise SourceUnavailableError(f"{failed}. Not the canonical schema: {problem}")

        origin = f"{name}:{started.isoformat(timespec='seconds')}|{connector.base_url}"
        digest = rows_hash(rows)
        source = Source(process_id=process_id, name=name, origin=origin, rows=rows)
        from app.features.versions.service import lock

        await lock(session, process_id)
        session.add(source)
        await session.flush()
        diff = await diff_latest(session, process_id, name, config.key)
        stats = connector.stats.__dict__
        span.set(
            source_id=source.id,
            origin=origin,
            rows=len(rows),
            rows_hash=digest,
            diff=diff.summary(),
            **stats,
        )
        await session.commit()
    if diff.added or diff.removed or diff.changed:
        from app.features.alerts.service import after_source_load

        await after_source_load(process_id, [name])
    return SyncResult(
        source_id=source.id,
        origin=origin,
        rows=len(rows),
        rows_hash=digest,
        stats=stats,
        diff=diff,
    )


async def sync_process(session: AsyncSession, process_id: int, name: str) -> SyncResult:
    config = await process_config(session, process_id, name)
    schema = (await process_schemas(session, process_id)).get(name)
    return await sync(session, process_id, name, config, schema=schema)


async def sync_before_run(session: AsyncSession, process_id: int) -> dict[str, str]:
    """Sync every live source of the process's use case (ADR 0028) and return the ones that
    failed, name -> why: down for this run. Each attempt is a `sync_source` span under the
    caller's (`run_process`, `reprocess`)."""
    down = {}
    for name, schema in (await process_schemas(session, process_id)).items():
        if not schema.sync_before_run:
            continue
        try:
            config = await process_config(session, process_id, name)
        except TraceError as e:  # a broken configuration is an outage too: trace it
            down[name] = e.message
            with (
                suppress(TraceError),
                events.span("sync_source", process_id=process_id, source=name),
            ):
                raise
            continue
        try:
            await sync(session, process_id, name, config, schema=schema)
        except Exception as e:  # noqa: BLE001 - whatever failed, the source is down
            await session.rollback()
            down[name] = str(e)[:1000]
    return down
