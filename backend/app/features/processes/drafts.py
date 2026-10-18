"""Saved discovery conversations. A reviewed import publishes all rules in one transaction."""

import asyncio
import copy
import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.common.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core import events
from app.features.agents import discovery
from app.features.decisions import service as decisions
from app.features.ingestion.model import File
from app.features.processes import draft_compilation as compilation
from app.features.processes import execution as execution_choices
from app.features.processes import service
from app.features.processes.draft_schemas import (
    AcceptanceExample,
    ConnectorProposal,
    DiscoveryDraftOut,
    DraftPlan,
    DraftStart,
    GuidanceProposal,
    ProposalEvidence,
    RuleProposal,
    SourceProposal,
)
from app.features.processes.model import (
    DecisionType,
    DiscoveryRevision,
    DiscoverySession,
    Process,
    Symbol,
)
from app.features.rules.model import NormRule, Rule
from app.features.sources import discovery as evidence_assets
from app.features.sources import service as sources
from app.features.sources.http_connector import (
    HttpConnector,
    HttpSourceConfig,
    SourcesFile,
    SyncError,
)
from app.features.sources.model import Source
from app.features.use_cases import service as use_cases
from app.features.use_cases.model import AgentConfig, UseCase
from app.features.versions import configuration as config
from app.features.versions import execution
from app.features.versions import service as versions
from app.features.versions.model import ProcessDraft


def manager(user):
    if user.role != "manager":
        raise PermissionDeniedError("Only a manager can configure a process")


async def read(session, draft_id, revision=None, *, lock=False):
    query = (
        select(DiscoverySession)
        .where(DiscoverySession.id == draft_id)
        .execution_options(populate_existing=True)
    )
    draft = await session.scalar(query.with_for_update() if lock else query)
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} does not exist")
    if revision is not None and (draft.revision != revision or draft.published_process_id):
        raise ConflictError("Draft changed or was published; reload before continuing")
    stored = await session.get(DiscoveryRevision, (draft.id, draft.revision))
    return draft, copy.deepcopy(stored.data)


async def connectors(session, draft):
    configured = {}
    if draft.process_id:
        version = await versions.active(session, draft.process_id, required=False)
        configured.update(
            {
                name: HttpSourceConfig.model_validate(row)
                for name, row in (version.snapshot.get("connectors", {}) if version else {}).items()
            }
        )
    if draft.use_case_id is not None:
        use_case = await session.get(UseCase, draft.use_case_id)
        try:
            path = sources.pack_sources_file(sources.find_pack(use_case.name))
        except NotFoundError:
            path = None
        if path and path.exists():
            packaged = SourcesFile.model_validate_json(path.read_text()).sources
            configured = {**packaged, **configured}
    return configured


async def output(session, draft_id):
    draft, data = await read(session, draft_id)
    return DiscoveryDraftOut(
        id=draft.id,
        revision=draft.revision,
        process_id=draft.process_id,
        published_process_id=draft.published_process_id,
        plan=data["plan"],
        reviews=data["reviews"],
        messages=data["messages"],
        documents=evidence_assets.inventory(data["documents"]),
        snapshots=[
            {"name": n, "origin": s["origin"], "rows": len(s["rows"])}
            for n, s in data["snapshots"].items()
        ],
        connectors=list(await connectors(session, draft)),
        preview=data.get("preview"),
        changes=changes(data),
        trace_id=data.get("trace_id"),
        execution=execution_choices.read(data) if "agents" in data else None,
    )


def changes(data):
    before = data.get("base_plan", {})
    return [
        {"field": key, "before": before.get(key), "after": value}
        for key, value in data["plan"].items()
        if key not in {"summary", "questions"} and value != before.get(key)
    ]


def plan_changed(before: dict, after: dict) -> bool:
    """A conversational answer is not a configuration edit just because its prose changed."""
    fields = (set(before) | set(after)) - {"summary", "questions"}
    return any(before.get(field) != after.get(field) for field in fields)


async def save(session, draft_id, revision, data, user, step):
    draft, _ = await read(session, draft_id, revision, lock=True)
    draft.revision += 1
    session.add(
        DiscoveryRevision(draft_id=draft.id, number=draft.revision, author_id=user.id, data=data)
    )
    point = {"draft_id": draft.id, "revision": draft.revision, "author": user.name}
    if (current := events.current()) and current.step == step:
        current.set(**point)  # the step's own span says it: one span, never counted twice
    else:
        events.record(session, step, process_id=draft.process_id, data=point)
    await session.commit()
    return await output(session, draft.id)


async def fingerprint(session, process_id):
    if process_id is None:
        return None
    process = await service.get(session, process_id)
    state = {
        "process": process.model_dump(),
        "inputs": await execution.capture(session, process_id),
    }
    return config.digest(state)


async def start(session, body: DraftStart, user):
    manager(user)
    plan = DraftPlan(name=body.name)
    snapshots, base_references = {}, []
    base = None
    if body.process_id is not None:
        await versions.lock(session, body.process_id)
        current = await service.get(session, body.process_id)
        base = copy.deepcopy((await versions.active(session, body.process_id)).snapshot)
        body.use_case_id = current.use_case_id
        plan = DraftPlan(**current.model_dump(exclude={"id", "use_case_id"}))
        ref = f"version:{current.active_version_id}"
        base_references.append(ref)
        plan.guidance = [
            GuidanceProposal(
                name=key,
                text=text,
                evidence=[
                    ProposalEvidence(reference=ref, explanation="Published reviewer guidance")
                ],
            )
            for key, text in base["guidance"].items()
        ]
        plan.examples = [
            AcceptanceExample.model_validate(e) for e in base.get("acceptance_examples", [])
        ]
        for rule in await decisions.active_rules(session, body.process_id):
            ref = f"rule:{rule.id}"
            base_references.append(ref)
            plan.rules.append(
                RuleProposal(
                    name=f"rule-{rule.id}",
                    text=rule.text,
                    type=rule.type,
                    decision=rule.decision,
                    evidence=[ProposalEvidence(reference=ref, explanation="Existing active rule")],
                )
            )
        for source in await sources.current_loads(session, body.process_id):
            snapshots[source.name] = {"origin": source.origin, "rows": source.rows}
            plan.sources.append(
                SourceProposal(
                    name=source.name,
                    kind="snapshot",
                    snapshot=source.name,
                    explanation="Existing source snapshot",
                    evidence=[
                        ProposalEvidence(
                            reference=f"snapshot:{source.name}",
                            explanation="Current process source",
                        )
                    ],
                )
            )
        for name, connector in base.get("connectors", {}).items():
            schema = base.get("source_schemas", {}).get(name, {})
            plan.connectors.append(
                ConnectorProposal(
                    name=name,
                    explanation="Existing published connector",
                    evidence=[
                        ProposalEvidence(
                            reference=f"version:{current.active_version_id}",
                            explanation="Published connector",
                        )
                    ],
                    config=connector,
                    required=schema.get("required", list(connector.get("fields", {}))),
                    optional=schema.get("optional", []),
                    sync_before_run=schema.get("sync_before_run", True),
                )
            )
    if body.use_case_id and await session.get(UseCase, body.use_case_id) is None:
        raise NotFoundError("Use case does not exist")
    draft = DiscoverySession(process_id=body.process_id, use_case_id=body.use_case_id, revision=1)
    session.add(draft)
    await session.flush()
    data = {
        "plan": plan.model_dump(),
        "base_plan": plan.model_dump(),
        "reviews": {},
        "messages": [],
        "documents": {},
        "snapshots": snapshots,
        "base_references": base_references,
        "base_configuration": base,
        "base_sources": {name: copy.deepcopy(source["rows"]) for name, source in snapshots.items()},
        "preview": None,
        "base_fingerprint": await fingerprint(session, body.process_id),
    }
    pinned = base or {"agents": await config.agents(session, body.use_case_id or 0)}
    choices = body.execution or execution_choices.read(pinned)
    execution_choices.write(data, choices)
    session.add(DiscoveryRevision(draft_id=draft.id, number=1, author_id=user.id, data=data))
    await session.commit()
    return await output(session, draft.id)


def invalidate(data):
    data["reviews"] = {}
    data["preview"] = None


async def conversation_context(session, draft, data):
    if not draft.process_id:
        return
    from app.features.learning import evidence

    snapshot = await evidence.capture(session, draft.process_id)
    if any(i["symbols"] is not None for i in snapshot["instances"]) and snapshot["decisions"]:
        data["case_context"] = await evidence.context(session, snapshot, 30)
    else:
        data["case_context"] = {"evidence": {}, "sampling": {"selected": 0}}
    data["case_context"]["active_version_id"] = snapshot["version_id"]
    data["case_context"]["published_configuration"] = snapshot["process"]
    current = await session.get(ProcessDraft, draft.process_id, populate_existing=True)
    data["version_draft"] = (
        {"revision": current.revision, "snapshot": current.snapshot} if current else None
    )


async def message(session, draft_id, body, user):
    manager(user)
    draft, data = await read(session, draft_id, body.revision)
    data["messages"].append(
        {"role": "user", "text": body.message, "author": user.name, "mode": body.mode}
    )
    await conversation_context(session, draft, data)
    setups = (
        config.setups(data)
        if "agents" in data
        else (await use_cases.setups(session, draft.use_case_id) if draft.use_case_id else {})
    )
    if body.mode == "discuss":
        with events.span("discuss_process", process_id=draft.process_id, draft_id=draft_id) as span:
            data["trace_id"] = span.trace_id
            answer = await discovery.discuss(data, setups.get("discovery"))
            data["messages"].append(
                {
                    "role": "assistant",
                    "text": answer.message,
                    "evidence": answer.evidence,
                    "questions": answer.questions,
                    "trace_id": span.trace_id,
                }
            )
            return await save(session, draft_id, body.revision, data, user, "discuss_process")
    with events.span("discover_process", draft_id=draft_id, author=user.name) as span:
        data["trace_id"] = span.trace_id
        plan = await discovery.discover(data, setups.get("discovery"))
    if not plan.name.strip():
        # A revision answers a question; it never renames the process. A model that omits
        # the name would otherwise blank it, and preparation would refuse the whole draft.
        plan.name = data["plan"].get("name", "")
    before_plan = data["plan"]
    data["plan"] = plan.model_dump()
    data["messages"].append({"role": "assistant", "text": plan.summary, "trace_id": span.trace_id})
    edited = plan_changed(before_plan, data["plan"])
    if edited:
        invalidate(data)
    if edited and draft.process_id:  # each change of an existing process, as a proposal to settle
        from app.features.proposals import service as proposals

        await proposals.from_chat(session, draft, data, body.revision + 1)
    return await save(session, draft_id, body.revision, data, user, "revise_process_draft")


async def upload(session, draft_id, revision, name, content, user):
    manager(user)
    _, data = await read(session, draft_id, revision)
    if len(content) > 20 * 1024 * 1024:
        raise ConflictError("Evidence asset exceeds 20 MB")
    if len(data["documents"]) >= 5:
        raise ConflictError("A draft supports up to five evidence assets")
    workbook = await asyncio.to_thread(evidence_assets.read_asset, name, content)
    digest = hashlib.sha256(content).hexdigest()
    await session.execute(
        insert(File)
        .values(hash=digest, name=name, content=content, text=None)
        .on_conflict_do_nothing(index_elements=[File.hash])
    )
    data["documents"][digest] = {"name": name, "workbook": workbook}
    data["messages"].append(
        {
            "role": "user",
            "text": f"Uploaded evidence asset {name}, reference {digest}",
            "author": user.name,
        }
    )
    invalidate(data)
    return await save(session, draft_id, revision, data, user, "upload_evidence_asset")


async def sync(session, draft_id, revision, name, user):
    manager(user)
    draft, data = await read(session, draft_id, revision)
    plan = DraftPlan.model_validate(data["plan"])
    proposed = next((row for row in plan.connectors if row.name == name), None)
    if proposed and data["reviews"].get(f"connector:{name}") != "accepted":
        raise ConflictError("Review and accept the connector before it contacts the source")
    configs = await connectors(session, draft)
    config_row = proposed.config if proposed else configs.get(name)
    if config_row is None:
        raise NotFoundError("This source is not a configured connector for the draft")
    connector = HttpConnector(config_row)
    with events.span("discover_source", draft_id=draft_id, source=name, author=user.name) as span:
        try:
            rows = await connector.download()
        except SyncError as error:
            raise sources.SourceUnavailableError(str(error)) from error
        span.set(**connector.stats.__dict__)
    data["snapshots"][name] = {
        "origin": f"discovery:{name}:{sources.rows_hash(rows)}",
        "rows": rows,
    }
    data["messages"].append(
        {"role": "user", "text": f"Loaded complete source snapshot:{name}", "author": user.name}
    )
    invalidate(data)
    return await save(session, draft_id, revision, data, user, "load_draft_source")


async def review(session, draft_id, body, user):
    manager(user)
    _, data = await read(session, draft_id, body.revision)
    if body.proposal not in compilation.proposals(DraftPlan.model_validate(data["plan"])):
        raise NotFoundError("Proposal does not exist in this revision")
    data["reviews"][body.proposal] = body.disposition
    data["preview"] = None
    data["messages"].append(
        {
            "role": "user",
            "text": f"{body.disposition}: {body.proposal}. {body.explanation}",
            "author": user.name,
        }
    )
    return await save(session, draft_id, body.revision, data, user, "review_draft_proposal")


async def check_base(session, draft, data, plan):
    if await fingerprint(session, draft.process_id) != data["base_fingerprint"]:
        raise ConflictError(
            "The process, sources or decisions changed; start a fresh draft to review their impact"
        )
    if draft.process_id:
        if await session.get(ProcessDraft, draft.process_id, populate_existing=True):
            raise ConflictError(
                "A process draft was created; finish it before discovery publication"
            )
        if await session.scalar(
            select(Rule.id)
            .where(Rule.process_id == draft.process_id, Rule.status == "compiling")
            .limit(1)
        ):
            raise ConflictError("Wait for the process's existing rule compilations to finish")
        current = await service.get(session, draft.process_id)
        if current.name != plan.name:
            raise ConflictError(
                "Keep the existing process name; this conversation revises its configuration"
            )


async def prepare(session, draft_id, revision, user):
    manager(user)
    draft, data = await read(session, draft_id, revision)
    plan = DraftPlan.model_validate(data["plan"])
    compilation.ready(plan, data["reviews"])
    await check_base(session, draft, data, plan)
    tables = evidence_assets.materialize(plan, data)
    setups = (
        config.setups(data)
        if "agents" in data
        else (await use_cases.setups(session, draft.use_case_id) if draft.use_case_id else {})
    )
    with events.span("compile_process_draft", draft_id=draft_id, author=user.name) as span:
        data["trace_id"] = span.trace_id
        # Every proposal, evidence, source and execution edit clears the preview. Its
        # valid artifacts therefore belong to this unchanged pinned candidate; repeating
        # checks only needs fresh examples and historical evidence, not paid compilation.
        cached = (data.get("preview") or {}).get("compilations")
        reuse = bool(cached) and all((item.get("report") or {}).get("valid") for item in cached)
        span.set(compilations_reused=reuse)
        compiled = (
            cached
            if reuse
            else await compilation.compile_plan(
                plan, tables, setups, data["base_configuration"], data.get("base_sources")
            )
        )
        preview_base = copy.deepcopy(data["base_configuration"])
        if preview_base is not None and "agents" in data:
            # Paired reviewer calls use the selected draft models on both inputs.
            # A local-only draft must never send a baseline call to an old cloud model.
            execution_choices.write(preview_base, execution_choices.read(data))
        data["preview"] = await compilation.preview(
            session, draft.process_id, plan, tables, compiled, preview_base
        )
        data["preview"]["source_mutations"] = evidence_assets.mutation_summary(plan, data, tables)
    if draft.process_id:
        await versions.lock(session, draft.process_id)
    await check_base(session, draft, data, plan)
    return await save(session, draft_id, revision, data, user, "preview_process_draft")


async def publish(session, draft_id, revision, user):
    manager(user)
    draft, _ = await read(session, draft_id, revision)
    if draft.process_id:
        await versions.lock(session, draft.process_id)
    draft, data = await read(session, draft_id, revision, lock=True)
    plan = DraftPlan.model_validate(data["plan"])
    compilation.ready(plan, data["reviews"])
    if not (data.get("preview") or {}).get("valid"):
        raise ConflictError(
            "Compile and pass the acceptance examples and backtest before publishing"
        )
    await check_base(session, draft, data, plan)
    tables = evidence_assets.materialize(plan, data)
    if draft.process_id:
        process = await session.get(Process, draft.process_id)
    else:
        if await session.scalar(select(Process.id).where(Process.name == plan.name)):
            raise ConflictError("A process with this name already exists")
        # New process owns its description; using an existing use case only supplies
        # model settings and configured connectors during discovery.
        use_case = UseCase(name=f"discovery-{draft.id}-{plan.name}", description=plan.description)
        session.add(use_case)
        await session.flush()
        if draft.use_case_id:
            for role, setup in (await use_cases.setups(session, draft.use_case_id)).items():
                session.add(
                    AgentConfig(
                        use_case_id=use_case.id,
                        role=role,
                        version=1,
                        active=True,
                        author=user.name,
                        config=setup.settings.model_dump(),
                        note="From discovery setup",
                    )
                )
        process = Process(name=plan.name, use_case_id=use_case.id)
        session.add(process)
        await session.flush()
        session.add_all(
            DecisionType(process_id=process.id, **t.model_dump()) for t in plan.decision_types
        )
        session.add_all(Symbol(process_id=process.id, **s.model_dump()) for s in plan.symbols)
        await session.flush()
    old = await decisions.active_rules(session, process.id) if draft.process_id else []
    published = []
    norms = {}
    new_proposals = {
        item["proposal"]
        for item in data["preview"]["compilations"]
        if item.get("existing_rule_id") is None
    }
    for number, proposal in enumerate(plan.rules, 1):
        if proposal.name not in new_proposals:
            continue
        norm = NormRule(process_id=process.id, number=number, text=proposal.text, policies=[])
        session.add(norm)
        await session.flush()
        norms[proposal.name] = norm.id
    for item in data["preview"]["compilations"]:
        if item.get("existing_rule_id") is not None:
            published.append(await session.get(Rule, item["existing_rule_id"]))
            continue
        rule = Rule(
            process_id=process.id,
            norm_rule_id=norms[item["proposal"]],
            text=item["text"],
            summary=item.get("summary"),
            type=item["type"],
            decision=item["decision"],
            code=item["code"],
            tests=item["tests"],
            hash=item["hash"],
            status="draft",
            report={
                **item["report"],
                "discovery": {
                    "draft_id": draft.id,
                    "revision": revision,
                    "proposal": item["proposal"],
                    "author": user.name,
                },
            },
        )
        session.add(rule)
        published.append(rule)
    await session.flush()
    base = data["base_configuration"] or await config.workspace(session, process.id)
    snapshot = compilation.candidate(plan, data["preview"]["compilations"], base)
    snapshot["rules"] = [config.artifact(rule) for rule in published]
    if "agents" in data:
        execution_choices.write(snapshot, execution_choices.read(data))
    impact = await versions.inspect(
        session,
        snapshot,
        await execution.capture(session, process.id),
        tables=tables,
    )
    if not impact["valid"]:
        raise ConflictError("The candidate failed version validation; prepare and review again")
    version = await versions.publish_snapshot(
        session,
        process,
        snapshot,
        user.name,
        f"Approved discovery {draft.id} revision {revision}",
        {
            **impact,
            "discovery_id": draft.id,
            "revision": revision,
            "examples": data["preview"]["examples"],
            "review": data["preview"].get("review"),
        },
    )
    data["published_version_id"] = version.id
    # Empty snapshots retire source names omitted from the proposal without deleting history.
    previous_names = {s.name for s in await sources.current_loads(session, process.id)}
    session.add_all(
        Source(
            process_id=process.id,
            name=name,
            rows=tables.get(name, []),
            origin=f"draft:{draft.id}:revision:{revision}",
        )
        for name in previous_names | tables.keys()
    )
    data["published_rule_ids"] = [r.id for r in published]
    data["retired_rule_ids"] = [r.id for r in old if r.id not in data["published_rule_ids"]]
    draft.published_process_id = process.id
    draft.revision += 1
    session.add(
        DiscoveryRevision(draft_id=draft.id, number=draft.revision, author_id=user.id, data=data)
    )
    events.record(
        session,
        "publish_process_draft",
        process_id=process.id,
        data={
            "draft_id": draft.id,
            "approved_revision": revision,
            "author": user.name,
            "published_rules": data["published_rule_ids"],
            "retired_rules": data["retired_rule_ids"],
        },
    )
    await session.commit()
    from app.features.alerts.service import after_publish

    await after_publish(process.id, version.id)
    return await output(session, draft.id)


async def configure_execution(session, draft_id, body, user):
    manager(user)
    _, data = await read(session, draft_id, body.revision)
    selected = body.execution.model_copy(update={"preset": "custom"})
    execution_choices.write(data, selected)
    invalidate(data)
    return await save(session, draft_id, body.revision, data, user, "configure_discovery_execution")
