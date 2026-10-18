import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, NotFoundError
from app.core import events
from app.core.config import settings
from app.core.events import Event
from app.features.agents import compiler, decision_reviewer, sandbox
from app.features.decisions import outcomes_file, runs
from app.features.decisions.engine import Outcomes, RunDataset, Verdict, decide
from app.features.decisions.model import ENGINE, Decision, DecisionReview, Finding
from app.features.decisions.schemas import (
    ChangeOut,
    DecisionOut,
    DecisionReviewOut,
    EventOut,
    FindingOut,
    InstanceDetail,
    InstanceOut,
    ProcessSummary,
    ReprocessSummary,
    ResolveIn,
    RuleSummary,
    RunSummary,
    SourceSummary,
)
from app.features.ingestion.model import Instance
from app.features.ingestion.symbols import flatten_symbols, scan, unverified
from app.features.processes.service import get as get_process
from app.features.proposals.model import ManagerProposal
from app.features.proposals.service import settle, supersede
from app.features.rules.model import Rule
from app.features.sources import service as sources
from app.features.sources.model import Source
from app.features.users.model import User
from app.features.versions import configuration as version_config
from app.features.versions import execution
from app.features.versions import service as versions
from app.features.versions.model import Execution, ProcessVersion

log = logging.getLogger(__name__)


async def _instance(session: AsyncSession, instance_id: int) -> Instance:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError(f"Instance {instance_id} does not exist")
    return instance


async def outcomes(session: AsyncSession, process_id: int) -> Outcomes:
    """The loader guarantees one default and at least one type that requires a human; the
    highest-priority one of those is where the engine sends what it cannot decide, including
    an instance missing a required symbol."""
    process = await get_process(session, process_id)
    return version_config.outcomes({"process": process.model_dump(mode="json")})


async def human_types(session: AsyncSession, process_id: int) -> list[str]:
    process = await get_process(session, process_id)
    return [t.name for t in process.decision_types if t.requires_human]


async def historical_human_types(session, process_id, decisions):
    fallback = set(await human_types(session, process_id))
    rows = await session.scalars(
        select(ProcessVersion).where(
            ProcessVersion.id.in_({d.version_id for d in decisions if d.version_id})
        )
    )
    by_version = {
        v.id: {t["name"] for t in v.snapshot["process"]["decision_types"] if t["requires_human"]}
        for v in rows
    }
    return {d.id: by_version.get(d.version_id, fallback) for d in decisions}


async def current_sources(session: AsyncSession, process_id: int) -> dict[str, list[dict]]:
    """The latest load of each source of truth. Every load is kept; only the last one is used."""
    return {load.name: load.rows for load in await sources.current_loads(session, process_id)}


async def instances_of(session: AsyncSession, process_id: int) -> list[Instance]:
    return list(
        await session.scalars(
            select(Instance).where(Instance.process_id == process_id).order_by(Instance.id)
        )
    )


async def latest_decisions(session: AsyncSession, instances: list[Instance]) -> dict[int, Decision]:
    """An instance's current decision is its latest row (the history only ever grows)."""
    if not instances:
        return {}
    rows = await session.scalars(
        select(Decision)
        .where(Decision.instance_id.in_([i.id for i in instances]))
        .distinct(Decision.instance_id)
        .order_by(Decision.instance_id, Decision.id.desc())
    )
    return {row.instance_id: row for row in rows}


async def active_rules(session: AsyncSession, process_id: int) -> list[Rule]:
    version = await versions.active(session, process_id)
    return version_config.rules(version.snapshot)


async def ready_rules(session: AsyncSession, process_id: int) -> list[Rule]:
    return await active_rules(session, process_id)


async def decide_all(
    session: AsyncSession,
    process_id: int,
    rules: list[Rule],
    selected: list[Instance],
    source_loads: list[Source] | None = None,
) -> list[Verdict]:
    """`selected` instances under `rules`, with the current sources and the whole population
    of the process as `others`. Each population entry carries `_instance`, its name, so a
    rule can name the duplicate it found; the sandbox leaves an instance out of its own
    `others`. Rule code gets symbols as plain values (ADR 0008)."""
    out = await outcomes(session, process_id)
    sources = (
        {s.name: s.rows for s in source_loads}
        if source_loads is not None
        else await current_sources(session, process_id)
    )
    population = [
        (i.id, {**flatten_symbols(i.symbols), "_instance": i.name})
        for i in await instances_of(session, process_id)
        if i.symbols is not None
    ]
    dataset = [(i.id, flatten_symbols(i.symbols)) for i in selected]
    scans = {i.id: u for i in selected if (u := scan(i.symbols)) is not None}
    # One subprocess per rule, off the event loop.
    return await asyncio.to_thread(
        decide,
        rules,
        out,
        dataset,
        sources,
        population,
        _traced(rules),
        scans,
        None,
        settings.decision_workers,
        {i.id: unverified(i.symbols) for i in selected},
    )


def _traced(rules: list[Rule]) -> RunDataset:
    """`sandbox.run_dataset` with one `evaluate_rule` span per rule: how long its code took
    over the whole dataset, and how many instances it fired on or failed. The engine stays
    pure; the clock is here."""
    by_code = {r.code: r.id for r in rules}

    def run_dataset(code: str, instances: list, sources: dict, population: list) -> list:
        with events.span("evaluate_rule", rule_id=by_code.get(code), instances=len(instances)) as s:
            answers = sandbox.run_dataset(code, instances, sources, population)
            fired = sum(isinstance(a, dict) and a.get("fires") is True for a in answers)
            s.set(fired=fired, errors=sum(isinstance(a, BaseException) for a in answers))
            return answers

    return run_dataset


def _causes(verdicts: list[Verdict]) -> dict[str, dict[str, int]]:
    """Escalations by cause: MISSING_DATA, RULE_ERROR, RULE_NEEDS_DATA, RULE_COMPILE_FAILED,
    RULE_CONFLICT, UNVERIFIED_DATA, SCAN_REVIEW, SOURCE_UNAVAILABLE."""
    causes: Counter[str] = Counter()
    for verdict in verdicts:
        causes.update(r.reason.split(" ", 1)[0] for r in verdict.results if r.fires is None)
        for cause in ("MISSING_DATA", "RULE_CONFLICT", "UNVERIFIED_DATA", "SCAN_REVIEW"):
            if verdict.reason.startswith(cause):
                causes[cause] += 1
    return {"failures": dict(causes)}


def _out(
    instance: Instance, decision: Decision | None, reviews: dict[int, DecisionReview] | None = None
) -> InstanceOut:
    review = (reviews or {}).get(decision.id) if decision else None
    return InstanceOut(
        id=instance.id,
        name=instance.name,
        status=instance.status,
        decision=decision.decision if decision else None,
        author=decision.author if decision else None,
        reason=decision.reason if decision else None,
        decided_at=decision.created_at if decision else None,
        review_pending=bool(review and review.requires_human),
        values=flatten_symbols(instance.symbols) if instance.symbols is not None else None,
    )


async def run(
    session: AsyncSession,
    process_id: int,
    author: str | None = None,
    *,
    instance_ids: list[int] | None = None,
    idempotency_key: str | None = None,
) -> RunSummary:
    """Decide every PENDING instance that already has its symbols.

    Runs against the rules active now and the latest load of each source. A rule never
    reads the clock: anything like a cut-off date is a row in a source of truth. An
    instance without symbols is not run and stays PENDING until extraction fills them.
    Live sources are synced first; one that fails is down for this run (ADR 0028).
    """
    from app.features.mail_ingestion.model import RunOperation

    selected = sorted(set(instance_ids)) if instance_ids is not None else None
    if selected is not None:
        owned = set(
            await session.scalars(
                select(Instance.id).where(
                    Instance.process_id == process_id, Instance.id.in_(selected)
                )
            )
        )
        if not selected or owned != set(selected):
            raise ConflictError("Select nonempty instance IDs belonging to this process")
    if idempotency_key and selected is None:
        raise ConflictError("Idempotency requires an explicit instance selection")

    async def previous():
        if not idempotency_key:
            return None
        operation = await session.scalar(
            select(RunOperation).where(
                RunOperation.process_id == process_id, RunOperation.key == idempotency_key
            )
        )
        if operation:
            if operation.instance_ids != selected:
                raise ConflictError("Idempotency key already used for another selection")
            return RunSummary.model_validate(operation.result)
        return None

    if saved := await previous():
        return saved
    with events.span("run_process", process_id=process_id) as span:
        down = await sources.sync_before_run(session, process_id)
        await versions.lock(session, process_id)
        if saved := await previous():
            return saved
        version = await versions.active(session, process_id)
        process = await get_process(session, process_id)
        rules = await ready_rules(session, process_id)
        pending = [
            i
            for i in await session.scalars(
                select(Instance)
                .where(
                    Instance.process_id == process_id,
                    Instance.status == "PENDING",
                )
                .order_by(Instance.id)
                .with_for_update()
            )
            if i.symbols is not None and (selected is None or i.id in selected)
        ]
        source_loads, inputs, down = await _inputs(session, process_id, down, version.snapshot)
        captured = Execution(process_id=process_id, version_id=version.id, inputs=inputs)
        session.add(captured)
        await session.flush()
        span.set(execution_id=captured.id, author=author)
        evaluated = await execution.evaluate(
            session, version.snapshot, inputs, [i.id for i in pending]
        )
        verdicts = [evaluated[i.id] for i in pending]
        stats = runs.outcomes(verdicts, version.snapshot)  # before commit expires version

        count: Counter[str] = Counter()
        for instance, verdict in zip(pending, verdicts, strict=True):
            decision = _append(
                session,
                process_id,
                instance,
                verdict,
                version_id=version.id,
                execution_id=captured.id,
            )
            if process.decision_review is not None:
                await session.flush()
                await decision_reviewer.assess(
                    session,
                    process,
                    instance,
                    decision,
                    rules,
                    source_loads,
                    snapshot=version.snapshot,
                )
            count[verdict.decision] += 1

        await session.flush()
        result = RunSummary(decided=sum(count.values()), by_decision=dict(count), down_sources=down)
        if selected is not None:
            result.execution_id = captured.id
            result.decision_ids = list(
                await session.scalars(
                    select(Decision.id)
                    .where(Decision.execution_id == captured.id)
                    .order_by(Decision.id)
                )
            )
        if idempotency_key:
            session.add(
                RunOperation(
                    process_id=process_id,
                    key=idempotency_key,
                    instance_ids=selected,
                    result=result.model_dump(mode="json"),
                )
            )
        await session.commit()
        span.set(
            instances=len(pending),
            rules=len(rules),
            by_decision=count,
            down_sources=down,
            **_causes(verdicts),
            **stats,
        )
    return result


async def _inputs(
    session: AsyncSession, process_id: int, down: dict[str, str], snapshot: dict
) -> tuple[list[Source], dict, dict[str, str]]:
    """The current loads and captured execution inputs, without the loads of `down`
    sources: a rule never reads an older snapshot of a source that failed to sync. A source
    a published rule reads by name that has no load at all in the process (no `Source`
    row: a workbook, cut-off or ERP never loaded) is down too, "never loaded": missing
    reference data is unknown, not an empty table. A load with no rows stays a load. The
    down sources are part of the inputs, so a replay decides the same (ADR 0028)."""
    current = await sources.current_loads(session, process_id)
    loaded = {s.name for s in current}
    # ponytail: a rule reading `*` (any source) is not matched; list its names if one appears.
    read = {n for r in version_config.rules(snapshot) for n in compiler.source_reads(r.code)}
    never = {n: "never loaded" for n in sorted(read - loaded - {compiler.ANY_SOURCE})}
    down = {**never, **down}  # a failed sync's error says more
    loads = [s for s in current if s.name not in down]
    inputs = await execution.capture(session, process_id)
    if down:
        kept = {s.id for s in loads}
        inputs["source_ids"] = [i for i in inputs["source_ids"] if i in kept]
        inputs["down"] = down
    return loads, inputs, down


def _append(
    session: AsyncSession,
    process_id: int,
    instance: Instance,
    verdict: Verdict,
    version_id: int,
    execution_id: int,
    **trace,
) -> Decision:
    """A new engine decision: a row added to the history, never an edit (ADR 0008)."""
    decision = Decision(
        instance_id=instance.id,
        version_id=version_id,
        execution_id=execution_id,
        decision=verdict.decision,
        results=[asdict(r) for r in verdict.results],
        rules_hash=verdict.rules_hash,
        author=ENGINE,
        reason=verdict.reason or None,
    )
    session.add(decision)
    instance.status = "DECIDED"
    events.record(
        session,
        "decision",
        process_id=process_id,
        instance_id=instance.id,
        data={
            "decision": verdict.decision,
            "rules_hash": verdict.rules_hash,
            "fired": [r.rule_id for r in verdict.results if r.fires],
            "reason": verdict.reason,
            **trace,
        },
    )
    return decision


async def reprocess(
    session: AsyncSession,
    process_id: int,
    names: list[str] | None,
    dry_run: bool = False,
    author: str | None = None,
) -> ReprocessSummary:
    """Decide the decided instances again with the rules active now and the latest sources.

    For a source resync, a corrected datum or a rule adopted after the fact. Only a
    decision that changes is written, as a new engine row: the old one stays in the
    history. A person's decision or an engine decision awaiting review is never replaced:
    where the engine would now say otherwise it is reported as a conflict.
    `names` limits it to those instance names (all decided instances if None); `dry_run`
    compares engine outcomes without writing or calling the optional reviewer.
    Like a run, it syncs the live sources first (ADR 0028). A dry run does not: it writes
    nothing, and it is what a sync's own alert detection calls (ADR 0026).
    """
    with events.span("reprocess", process_id=process_id, dry_run=dry_run) as span:
        down = {} if dry_run else await sources.sync_before_run(session, process_id)
        await versions.lock(session, process_id)
        version = await versions.active(session, process_id)
        process = await get_process(session, process_id)
        rules = await ready_rules(session, process_id)
        query = (
            select(Instance)
            .where(Instance.process_id == process_id, Instance.status == "DECIDED")
            .order_by(Instance.id)
            .with_for_update()
        )
        if names is not None:
            query = query.where(Instance.name.in_(names))
        selected = [i for i in await session.scalars(query) if i.symbols is not None]
        latest = await latest_decisions(session, selected)
        reviews = await decision_reviewer.for_decisions(session, list(latest.values()))
        source_loads, inputs, down = await _inputs(session, process_id, down, version.snapshot)
        captured = Execution(process_id=process_id, version_id=version.id, inputs=inputs)
        if not dry_run:
            session.add(captured)
            await session.flush()
            span.set(execution_id=captured.id, author=author)
        evaluated = await execution.evaluate(
            session, version.snapshot, inputs, [i.id for i in selected]
        )
        verdicts = [evaluated[i.id] for i in selected]
        stats = runs.outcomes(verdicts, version.snapshot)  # before commit expires version

        unchanged = 0
        changed: list[ChangeOut] = []
        conflicts: list[ChangeOut] = []
        for instance, verdict in zip(selected, verdicts, strict=True):
            previous = latest[instance.id]
            review = reviews.get(previous.id)
            if verdict.decision == previous.decision:
                unchanged += 1
                continue
            change = ChangeOut(
                instance_id=instance.id,
                name=instance.name,
                before=previous.decision,
                after=verdict.decision,
                previous_author=previous.author,
                reason=verdict.reason,
            )
            if previous.author != ENGINE or (review and review.requires_human):
                conflicts.append(change)
                continue
            changed.append(change)
            if not dry_run:
                decision = _append(
                    session,
                    process_id,
                    instance,
                    verdict,
                    version_id=version.id,
                    execution_id=captured.id,
                    reprocess=True,
                    previous=previous.id,
                )
                if process.decision_review is not None:
                    await session.flush()
                    await decision_reviewer.assess(
                        session,
                        process,
                        instance,
                        decision,
                        rules,
                        source_loads,
                        snapshot=version.snapshot,
                    )
        await (session.rollback() if dry_run else session.commit())
        span.set(
            instances=len(selected),
            rules=len(rules),
            unchanged=unchanged,
            changed=len(changed),
            conflicts=len(conflicts),
            down_sources=down,
            **_causes(verdicts),
            **stats,
        )
    return ReprocessSummary(
        unchanged=unchanged, changes=changed, conflicts=conflicts, down_sources=down
    )


async def list_instances(
    session: AsyncSession,
    process_id: int,
    status: str | None = None,
    decision: str | None = None,
    q: str | None = None,
) -> list[InstanceOut]:
    """Every instance, oldest first. `decision` filters on the latest decision; `q` is a
    case-insensitive match on the name."""
    await get_process(session, process_id)
    instances = await instances_of(session, process_id)
    latest = await latest_decisions(session, instances)
    reviews = await decision_reviewer.for_decisions(session, list(latest.values()))
    rows = [_out(i, latest.get(i.id), reviews) for i in instances]
    if status is not None:
        rows = [r for r in rows if r.status == status]
    if decision is not None:
        rows = [r for r in rows if r.decision == decision]
    if q:
        rows = [r for r in rows if q.casefold() in r.name.casefold()]
    return rows


async def summary(session: AsyncSession, process_id: int) -> ProcessSummary:
    """The numbers of a process page. Counts come from each instance's latest decision;
    a rule's `fires` from the latest engine decision only, since a person's carries no
    rule results."""
    process = await get_process(session, process_id)
    instances = await instances_of(session, process_id)
    latest = await latest_decisions(session, instances)
    human = await historical_human_types(session, process_id, list(latest.values()))

    by_status = Counter(i.status for i in instances)
    by_decision = Counter(d.decision for d in latest.values())
    reviews = await decision_reviewer.for_decisions(session, list(latest.values()))
    queue = sum(
        1
        for d in latest.values()
        if d.decision in human[d.id] or (d.id in reviews and reviews[d.id].requires_human)
    )
    resolved = sum(1 for d in latest.values() if d.author != ENGINE)

    # The rule results live in the engine's latest row per instance.
    engine_rows: dict[int, Decision] = {}
    if instances:
        rows = await session.scalars(
            select(Decision)
            .where(Decision.instance_id.in_([i.id for i in instances]), Decision.author == ENGINE)
            .distinct(Decision.instance_id)
            .order_by(Decision.instance_id, Decision.id.desc())
        )
        engine_rows = {row.instance_id: row for row in rows}
    fires: Counter[int] = Counter()
    for row in engine_rows.values():
        fires.update(r["rule_id"] for r in row.results if r.get("fires"))
    rules = await session.scalars(
        select(Rule).where(Rule.process_id == process_id).order_by(Rule.id)
    )
    last_run = max((row.created_at for row in engine_rows.values()), default=None)

    return ProcessSummary(
        id=process.id,
        name=process.name,
        instances=len(instances),
        by_status=dict(by_status),
        by_decision=dict(by_decision),
        queue=queue,
        resolved=resolved,
        rules=[
            RuleSummary(
                id=r.id,
                text=r.text,
                type=r.type,
                decision=r.decision,
                status=r.status,
                fires=fires[r.id],
            )
            for r in rules
        ],
        sources=[
            SourceSummary(
                id=load.id,
                name=load.name,
                origin=load.origin,
                rows=len(load.rows),
                loaded_at=load.loaded_at,
            )
            for load in await sources.current_loads(session, process_id)
        ],
        last_run_at=last_run,
    )


async def list_events(
    session: AsyncSession,
    process_id: int,
    step: str | None = None,
    instance_id: int | None = None,
    limit: int = 200,
) -> list[EventOut]:
    """The trace of a process, newest first: runs, resolutions, compilations, syncs, uploads,
    and the changes to how its use case's agents work (who, which version before and after)."""
    process = await get_process(session, process_id)
    # ponytail: the use case's spans are matched on JSONB, not indexed; a `use_case_id`
    # column on events if this feed gets slow.
    of_use_case = (Event.process_id.is_(None)) & (
        Event.data["use_case_id"].as_integer() == process.use_case_id
    )
    query = select(Event).where(or_(Event.process_id == process_id, of_use_case))
    if step is not None:
        query = query.where(Event.step == step)
    if instance_id is not None:
        query = query.where(Event.instance_id == instance_id)
    rows = await session.scalars(query.order_by(Event.id.desc()).limit(limit))
    return [EventOut.model_validate(e, from_attributes=True) for e in rows]


async def queue(session: AsyncSession, process_id: int, type: str | None) -> list[InstanceOut]:
    """Human outcomes and pending reviews. An explicit type filters the current outcome."""
    await get_process(session, process_id)

    instances = await instances_of(session, process_id)
    latest = await latest_decisions(session, instances)
    reviews = await decision_reviewer.for_decisions(session, list(latest.values()))
    wanted = await historical_human_types(session, process_id, list(latest.values()))
    return [
        _out(i, latest[i.id], reviews)
        for i in instances
        if i.id in latest
        and (
            latest[i.id].decision in ({type} if type else wanted[latest[i.id].id])
            or (
                type is None
                and latest[i.id].id in reviews
                and reviews[latest[i.id].id].requires_human
            )
        )
    ]


async def get_instance(session: AsyncSession, instance_id: int) -> InstanceDetail:
    instance = await _instance(session, instance_id)
    decisions = list(
        await session.scalars(
            select(Decision).where(Decision.instance_id == instance_id).order_by(Decision.id)
        )
    )
    rows = await session.scalars(
        select(Event).where(Event.instance_id == instance_id).order_by(Event.id)
    )
    reviews = await decision_reviewer.for_decisions(session, decisions)
    return InstanceDetail(
        **_out(instance, decisions[-1] if decisions else None, reviews).model_dump(),
        file_hash=instance.file_hash,
        symbols=instance.symbols,
        decisions=[DecisionOut.model_validate(d, from_attributes=True) for d in decisions],
        events=[EventOut.model_validate(e, from_attributes=True) for e in rows],
        reviews=[
            DecisionReviewOut.model_validate(r, from_attributes=True) for r in reviews.values()
        ],
    )


async def resolve(
    session: AsyncSession, instance_id: int, data: ResolveIn, user: User
) -> InstanceDetail:
    """A person's decision is a new row, never an edit of the engine's (ADR 0008)."""
    instance = await _instance(session, instance_id)
    await versions.lock(session, instance.process_id)
    await session.refresh(instance, with_for_update=True)
    previous = (await latest_decisions(session, [instance])).get(instance.id)
    out = await outcomes(session, instance.process_id)
    if previous and previous.version_id:
        version = await session.get(ProcessVersion, previous.version_id)
        out = version_config.outcomes(version.snapshot)
    if data.decision not in out.priorities:
        raise ConflictError(f"{data.decision!r} is not a decision type of this process")

    previous = (await latest_decisions(session, [instance])).get(instance.id)
    proposal = None
    if data.proposal_id is not None:
        proposal = await session.get(ManagerProposal, data.proposal_id, with_for_update=True)
        if proposal is None or proposal.instance_id != instance.id or proposal.status != "open":
            raise ConflictError(f"Proposal {data.proposal_id} is not open for this instance")
        if previous is None or previous.id != proposal.payload["decision_id"]:
            raise ConflictError("The case changed since the proposal; ask for a new one")
    row = Decision(
        instance_id=instance.id,
        decision=data.decision,
        results=[],  # a person decides on the evidence, not by running the rules
        rules_hash=previous.rules_hash if previous else "",
        version_id=previous.version_id
        if previous
        else (await versions.active(session, instance.process_id)).id,
        execution_id=previous.execution_id if previous else None,
        author=user.name,
        reason=data.reason,
    )
    session.add(row)
    if proposal:
        await session.flush()
        accepted = data.decision == proposal.payload["proposed"]
        outcome = {"decision_id": row.id, "decision": data.decision}
        settle(proposal, "accepted" if accepted else "rejected", user.name, outcome)
    instance.status = "DECIDED"
    events.record(
        session,
        "resolution",
        process_id=instance.process_id,
        instance_id=instance.id,
        data={
            "decision": data.decision,
            "author": user.name,
            "reason": data.reason,
            "before": previous.decision if previous else None,
            "previous_author": previous.author if previous else None,
            **({"proposal_id": proposal.id} if proposal else {}),
        },
    )
    # A resolution never waits on a proposal: what is still open on this case answers a
    # decision that is no longer the last, and an open rule suggestion on another case was
    # ignored (ADR 0035).
    await supersede(session, "case_changed", ManagerProposal.instance_id == instance.id)
    await supersede(
        session,
        "ignored",
        ManagerProposal.process_id == instance.process_id,
        ManagerProposal.channel == "escalation",
        ManagerProposal.kind == "rule",
        ManagerProposal.instance_id != instance.id,
    )
    await session.commit()
    return await get_instance(session, instance_id)


async def export(
    session: AsyncSession,
    process_id: int,
    names: set[str] | None = None,
    trace: bool = False,
    full: bool = False,
) -> tuple[str, list[str]]:
    """`outcomes.jsonl` for the challenge: one line per instance name, nothing else.

    Returns the body and the names shared by several instances. `file_id` is the filename
    exactly as supplied, accents included. Decisions without optional review retain the
    challenge's engine-first export. Reviewed decisions wait for pending approval and
    export a later human resolution when present (ADR 0021). A pending instance uses the
    process's escalation outcome, so an incomplete batch is still deliverable without
    creating a fake engine decision. `names` limits the export to one delivery batch;
    other instances may still be pending. `trace` adds a `trace_url` per line, the console
    screen that opens that case's trace; `full` adds the whole trace of the decision inline,
    so the file stands on its own.
    """
    with events.span("export_outcomes", process_id=process_id, batch=len(names or ())) as span:
        body, duplicates = await _export(session, process_id, names, trace, full)
        span.set(lines=body.count("\n") + 1 if body else 0, duplicates=len(duplicates))
        return body, duplicates


async def _export(
    session: AsyncSession, process_id: int, names: set[str] | None, trace: bool, full: bool = False
) -> tuple[str, list[str]]:
    await get_process(session, process_id)
    # Two files can share a name: only the most recent instance of each name is exported.
    by_name: dict[str, Instance] = {}
    times: Counter[str] = Counter()
    for instance in await instances_of(session, process_id):  # ordered by id
        if names is not None and instance.name not in names:
            continue
        by_name[instance.name] = instance
        times[instance.name] += 1
    duplicates = [name for name, n in times.items() if n > 1]
    if duplicates:
        log.warning("Process %s: duplicate names on export: %s", process_id, duplicates)
    instances = list(by_name.values())

    pending = [i for i in instances if i.status == "PENDING"]

    engine: dict[int, Decision] = {}
    human: dict[int, Decision] = {}
    if instances:
        rows = await session.scalars(
            select(Decision)
            .where(Decision.instance_id.in_([i.id for i in instances]))
            .order_by(Decision.id)
        )
        for row in rows:
            (engine if row.author == ENGINE else human)[row.instance_id] = row
    exported: dict[int, tuple[str, str | None]] = {
        instance_id: (decision.decision, decision.reason)
        for instance_id, decision in {**human, **engine}.items()
    }
    # A full line says who decided the case, so it exports the latest decision: a person's
    # resolution when it came after the engine's (the delivered file stays engine-first,
    # ADR 0009).
    chosen: dict[int, Decision] = {}
    if full:
        for instance in instances:
            automatic, resolution = engine.get(instance.id), human.get(instance.id)
            latest = (
                resolution
                if resolution and (not automatic or resolution.id > automatic.id)
                else automatic
            )
            if latest:
                chosen[instance.id] = latest
                exported[instance.id] = (latest.decision, latest.reason)
    reviews = await decision_reviewer.for_decisions(session, list(engine.values()))
    awaiting = []
    for instance in instances:
        automatic = engine.get(instance.id)
        review = reviews.get(automatic.id) if automatic else None
        if review is None:
            continue  # Preserve the challenge export for decisions without an optional review.
        resolution = human.get(instance.id)
        if resolution and resolution.id > automatic.id:
            exported[instance.id] = (resolution.decision, resolution.reason)
        elif review.requires_human:
            awaiting.append(instance.name)
    if awaiting:
        raise ConflictError(
            f"{len(awaiting)} instances awaiting human review: {', '.join(awaiting[:5])}"
        )

    if pending:
        escalation = (await outcomes(session, process_id)).escalate
        for instance in pending:
            exported.setdefault(instance.id, (escalation, None))

    undecided = [i.name for i in instances if i.id not in exported]
    if undecided:
        raise ConflictError(f"{len(undecided)} undecided instances: {', '.join(undecided[:5])}")
    # `reason` is the rule's own reason code, as the engine recorded it (ADR 0002): no model
    # is asked for it. The challenge allows trace fields beside the two required ones.
    extra = await _trace_rows(session, process_id, instances, chosen) if full else {}
    body = "\n".join(
        json.dumps(
            {"file_id": i.name, "result": exported[i.id][0]}
            | (extra.get(i.id) or {"reason": exported[i.id][1] or "NO_FINDING"})
            | ({"trace_url": outcomes_file.trace_url(process_id, i.name)} if trace or full else {}),
            ensure_ascii=False,
        )
        for i in instances
    )
    return body, duplicates


async def _trace_rows(
    session: AsyncSession, process_id: int, instances: list[Instance], chosen: dict[int, Decision]
) -> dict[int, dict]:
    """The whole trace of every exported line, in four queries for the batch, never one per
    instance. Every value is already in the database (ADR 0018): nothing is decided again."""
    numbers = dict(
        (
            await session.execute(
                select(ProcessVersion.id, ProcessVersion.number).where(
                    ProcessVersion.id.in_({d.version_id for d in chosen.values() if d.version_id})
                )
            )
        ).all()
    )
    rule_ids = {r["rule_id"] for d in chosen.values() for r in d.results if r.get("fires")}
    rules = {
        row.id: (row.summary or row.text, row.decision)
        for row in (
            await session.execute(
                select(Rule.id, Rule.summary, Rule.text, Rule.decision).where(Rule.id.in_(rule_ids))
            )
        ).all()
    }
    # The span each decision was taken in: its trace holds the reading, the run and the rules.
    traces = dict(
        (
            await session.execute(
                select(Event.instance_id, Event.trace_id)
                .where(
                    Event.instance_id.in_([i.id for i in instances]),
                    Event.step.in_(("decision", "resolution")),
                )
                .order_by(Event.id)
            )
        ).all()
    )
    # The last sync of every source of the process: what the rules read, as `GET /traces`.
    source = Event.data["source"].astext
    sources_read = [
        {
            "source": row.data["source"],
            "status": row.status,
            **{k: row.data.get(k) or 0 for k in ("requests", "retries")},
        }
        for row in (
            await session.execute(
                select(Event.data, Event.status)
                .where(Event.step == "sync_source", Event.process_id == process_id)
                .distinct(source)
                .order_by(source, Event.id.desc())
            )
        ).all()
    ]
    return {
        i.id: outcomes_file.trace_fields(
            author=d.author,
            reason=d.reason,
            decided_at=d.created_at.isoformat().replace("+00:00", "Z"),
            results=d.results,
            rules_hash=d.rules_hash,
            process_version=numbers.get(d.version_id),
            rules=rules,
            symbols=i.symbols,
            sources_read=sources_read,
            trace_id=traces.get(i.id),
        )
        for i in instances
        if (d := chosen.get(i.id))
    }


async def list_findings(session: AsyncSession, process_id: int) -> list[FindingOut]:
    """Past decisions a later rule says were wrong. A notice, never a correction."""
    await get_process(session, process_id)
    rows = await session.scalars(
        select(Finding)
        .join(Decision, Finding.decision_id == Decision.id)
        .join(Instance, Decision.instance_id == Instance.id)
        .where(Instance.process_id == process_id)
        .order_by(Finding.id)
    )
    return [FindingOut.model_validate(r, from_attributes=True) for r in rows]
