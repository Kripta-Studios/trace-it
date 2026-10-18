"""The single publication path for configuration, rules and learned norms."""

from copy import deepcopy

from sqlalchemy import select

from app.common.exceptions import ConflictError, NotFoundError
from app.core import events
from app.features.agents import compiler, sandbox
from app.features.decisions.model import Decision, Finding
from app.features.processes import execution as execution_config
from app.features.processes.model import Process
from app.features.processes.schemas import ProcessDetail
from app.features.proposals.model import ManagerProposal
from app.features.proposals.service import supersede
from app.features.rules.model import Rule
from app.features.versions import configuration as config
from app.features.versions import execution
from app.features.versions.model import ProcessDraft, ProcessVersion
from app.features.versions.schemas import DraftIn


async def lock(session, process_id: int) -> Process:
    row = await session.scalar(
        select(Process)
        .where(Process.id == process_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    return row


async def active(session, process_id: int, *, required: bool = True) -> ProcessVersion | None:
    process = await session.get(Process, process_id)
    if process is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    version = (
        await session.get(ProcessVersion, process.active_version_id)
        if process.active_version_id
        else None
    )
    if required and version is None:
        raise ConflictError("Publish an approved process version before running cases")
    return version


async def get_draft(session, process_id: int) -> ProcessDraft:
    draft = await session.get(ProcessDraft, process_id, populate_existing=True)
    if draft is None:
        raise NotFoundError("This process has no draft")
    return draft


async def stage(session, process_id: int, snapshot: dict, author: str) -> ProcessDraft:
    process = await lock(session, process_id)
    draft = await session.get(ProcessDraft, process_id, populate_existing=True)
    if draft is None:
        draft = ProcessDraft(
            process_id=process_id, revision=0, base_version_id=process.active_version_id
        )
        session.add(draft)
    draft.revision += 1
    draft.snapshot = deepcopy(snapshot)
    draft.snapshot["process"].pop("active_version_id", None)
    draft.author = author
    draft.validation = None
    await session.flush()
    return draft


async def edit(session, process_id: int, body: DraftIn, author: str) -> ProcessDraft:
    process = await lock(session, process_id)
    draft = await session.get(ProcessDraft, process_id, populate_existing=True)
    if not draft and body.expected_revision is not None:
        raise ConflictError("The previous draft was published or discarded; start a new draft")
    if draft and body.expected_revision != draft.revision:
        raise ConflictError("Supply the current expected_revision to edit this draft")
    if draft and draft.base_version_id != process.active_version_id:
        if body.restore_version_id != process.active_version_id:
            raise ConflictError(
                "The active version changed; restore its version into the draft and reapply edits"
            )
        draft.base_version_id = process.active_version_id
    version = await active(session, process_id, required=False)
    snapshot = deepcopy(
        draft.snapshot
        if draft
        else version.snapshot
        if version
        else await config.workspace(session, process_id)
    )
    if body.restore_version_id is not None:
        old = await session.get(ProcessVersion, body.restore_version_id)
        if old is None or old.process_id != process_id:
            raise NotFoundError("Version does not belong to this process")
        snapshot = deepcopy(old.snapshot)
    for name in ("description", "decision_types", "symbols", "decision_review"):
        if name in body.model_fields_set:
            if getattr(body, name) is None and name != "decision_review":
                raise ConflictError(f"{name} cannot be null")
            snapshot["process"][name] = body.model_dump(mode="json")[name]
    if body.acceptance_examples is not None:
        snapshot["acceptance_examples"] = body.acceptance_examples
    if body.rule_ids is not None:
        rows = list(
            await session.scalars(
                select(Rule)
                .where(Rule.process_id == process_id, Rule.id.in_(body.rule_ids))
                .order_by(Rule.id)
            )
        )
        if len(rows) != len(set(body.rule_ids)):
            raise ConflictError("Every selected rule must belong to this process")
        snapshot["rules"] = [config.artifact(r) for r in rows]
    if body.guidance is not None:
        if any(not key.strip() or not value.strip() for key, value in body.guidance.items()):
            raise ConflictError("Guidance references and text must be nonempty")
        snapshot["guidance"] = body.guidance
    if body.refresh_agents:
        snapshot["agents"] = await config.agents(session, snapshot["process"]["use_case_id"])
    if body.execution is not None:
        selected = body.execution.model_copy(deep=True)
        if selected.preset != "custom":
            try:
                current = execution_config.read(snapshot)
                expected = execution_config.preset(current, selected.preset)
            except (ValueError, TypeError, KeyError):
                selected.preset = "custom"
            else:
                if selected != expected and selected != current:
                    selected.preset = "custom"
        execution_config.write(snapshot, selected)
    elif "execution" in body.model_fields_set:
        raise ConflictError("execution cannot be null")
    elif "execution" not in snapshot:
        execution_config.write(snapshot, execution_config.read(snapshot))
    elif body.refresh_agents:
        snapshot["execution"]["preset"] = "custom"
        try:
            execution_config.read(snapshot)
        except ValueError as error:
            raise ConflictError(
                "Refreshed defaults conflict with this execution configuration"
            ) from error
    result = await stage(session, process_id, snapshot, author)
    await session.commit()
    return result


def check_configuration(snapshot: dict) -> None:
    detail = ProcessDetail.model_validate(snapshot["process"])
    if not detail.name.strip():
        raise ValueError("Process name must not be blank")
    if any(not t.name.strip() for t in detail.decision_types):
        raise ValueError("Decision type names must not be blank")
    if any(not s.name.strip() for s in detail.symbols):
        raise ValueError("Symbol names must not be blank")
    types = detail.decision_types
    names = {t.name for t in types}
    if len(names) != len(types) or len({t.priority for t in types}) != len(types):
        raise ValueError("Decision names and priorities must be unique")
    if (
        sum(t.is_default for t in types) != 1
        or any(t.is_default and t.requires_human for t in types)
        or not any(t.requires_human for t in types)
    ):
        raise ValueError("Exactly one automatic default and a human escalation type are required")
    symbols = {s.name for s in detail.symbols}
    if len(symbols) != len(detail.symbols):
        raise ValueError("Symbol names must be unique")
    if len({r["id"] for r in snapshot["rules"]}) != len(snapshot["rules"]):
        raise ValueError("Rule IDs must be unique")
    for rule in snapshot["rules"]:
        if rule["decision"] not in names:
            raise ValueError(f"Rule {rule['id']} refers to an unknown decision type")
        if not rule["code"] or not (rule["report"] or {}).get("valid"):
            raise ValueError(f"Rule {rule['id']} has no validated code")
        sandbox.check(rule["code"])
        unknown, _ = compiler.read_keys(rule["code"])
        if unknown - symbols:
            raise ValueError(f"Rule {rule['id']} reads removed or unknown symbols")
        if rule["tests"] and not all(
            t["passed"] for t in compiler.run_tests(rule["code"], rule["tests"])
        ):
            raise ValueError(f"Rule {rule['id']} fails its stored tests")
    config.setups(snapshot)
    from app.features.sources.http_connector import HttpSourceConfig
    from app.features.sources.service import SourceSchema

    connectors = {
        name: HttpSourceConfig.model_validate(row)
        for name, row in snapshot.get("connectors", {}).items()
    }
    schemas = {
        name: SourceSchema.model_validate(row)
        for name, row in snapshot.get("source_schemas", {}).items()
    }
    if connectors.keys() != schemas.keys():
        raise ValueError("Every published connector needs exactly one source schema")
    for name, connector in connectors.items():
        missing = set(schemas[name].required) - connector.fields.keys()
        if missing:
            raise ValueError(f"Connector {name!r} does not map required fields {sorted(missing)}")
    if "execution" in snapshot:
        execution_config.read(snapshot)


def _validation_missing_result(result) -> bool:
    return result.fires is None and result.reason.startswith(
        f"RULE_ERROR {result.rule_id}: MissingValidationSymbol: "
    )


async def _check_schema_examples(snapshot: dict, newly_required: set[str], tables: dict) -> dict:
    """Validate proposed outcomes with new required data present and absent."""
    import asyncio

    from app.features.decisions.engine import decide
    from app.features.processes.draft_schemas import AcceptanceExample

    try:
        examples = [
            AcceptanceExample.model_validate(row) for row in snapshot.get("acceptance_examples", [])
        ]
    except ValueError as error:
        return {"valid": False, "error": f"Invalid acceptance example: {error}"}
    required = {
        symbol["name"] for symbol in snapshot["process"]["symbols"] if symbol.get("required", False)
    }

    def has_value(example, name):
        value = example.instance.get(name)
        return value is not None and bool(str(value).strip())

    present = {
        name
        for name in newly_required
        if any(all(has_value(example, field) for field in required) for example in examples)
    }
    absent = {
        name
        for name in newly_required
        if any(
            not has_value(example, name)
            and all(has_value(example, field) for field in required - {name})
            for example in examples
        )
    }
    if present != newly_required or absent != newly_required:
        return {
            "valid": False,
            "error": "Supply accepted examples with each new required symbol present and absent",
            "missing_present": sorted(newly_required - present),
            "missing_absent": sorted(newly_required - absent),
        }
    rules = config.rules(snapshot)
    outcomes = config.outcomes(snapshot)
    for example in examples:
        missing = [
            name
            for name in newly_required
            if (value := example.instance.get(name)) is None or not str(value).strip()
        ]
        dataset = [(0, example.instance)]
        population = [*dataset, *((i + 1, row) for i, row in enumerate(example.others))]

        def run_dataset(code, instances, sources, others, missing=missing):
            return sandbox.run_dataset(
                code,
                instances,
                sources,
                others,
                **({"validation_missing": {0: missing}} if missing else {}),
            )

        verdict = (
            await asyncio.to_thread(
                decide,
                rules,
                outcomes,
                dataset,
                {**tables, **example.sources},
                population,
                run_dataset,
            )
        )[0]
        failures = [
            result.reason
            for result in verdict.results
            if result.fires is None and not _validation_missing_result(result)
        ]
        if verdict.decision != example.decision or failures:
            return {
                "valid": False,
                "error": f"Acceptance example {example.name!r} failed",
                "expected": example.decision,
                "actual": verdict.decision,
                "rule_errors": failures,
            }
    return {"valid": True, "tested": len(examples)}


async def inspect(session, snapshot: dict, inputs: dict, *, tables: dict | None = None) -> dict:
    # Validate artifacts even if the process has no past cases.
    import asyncio

    try:
        await asyncio.to_thread(check_configuration, snapshot)
    except (ValueError, sandbox.SandboxError) as error:
        return {"valid": False, "error": str(error)}
    process_id = snapshot["process"]["id"]
    baseline = await active(session, process_id, required=False)
    published_names = (
        {symbol["name"] for symbol in baseline.snapshot["process"]["symbols"]}
        if baseline
        else set()
    )
    newly_required = {
        symbol["name"]
        for symbol in snapshot["process"]["symbols"]
        if symbol.get("required", False) and symbol["name"] not in published_names
    }

    def absent(symbols: dict, name: str) -> bool:
        field = symbols.get(name)
        value = field.get("value") if isinstance(field, dict) else None
        return value is None or not str(value).strip()

    validation_missing = {
        instance["id"]: sorted(name for name in newly_required if absent(instance["symbols"], name))
        for instance in inputs["instances"]
        if instance["symbols"] is not None
    }
    validation_missing = {key: names for key, names in validation_missing.items() if names}
    selected = [i for i in inputs["instances"] if i["decision"] and i["symbols"] is not None]
    verdicts = await execution.evaluate(
        session,
        snapshot,
        inputs,
        [i["id"] for i in selected],
        tables=tables,
        validation_missing=validation_missing,
    )
    changes, conflicts, errors, not_evaluable, already_escalated = [], [], [], [], []
    resolved_by_person = []
    escalate = config.outcomes(snapshot).escalate
    unchanged = 0
    for instance in selected:
        # R01: compare with the engine's last word; a person's resolution never blocks.
        latest = instance["decision"]
        previous = instance.get("engine_decision") or latest
        verdict = verdicts[instance["id"]]
        missing = validation_missing.get(instance["id"], [])
        unavailable = []
        real_failures = []
        for result in verdict.results:
            marker = f"RULE_ERROR {result.rule_id}: MissingValidationSymbol: "
            if _validation_missing_result(result):
                unavailable.append(
                    {"rule_id": result.rule_id, "symbol": result.reason[len(marker) :]}
                )
            elif result.fires is None:
                real_failures.append(result.reason)
        old_required = {
            symbol["name"]
            for symbol in snapshot["process"]["symbols"]
            if symbol.get("required", False)
            and symbol["name"] in published_names
            and absent(instance["symbols"], symbol["name"])
        }
        if missing or unavailable:
            not_evaluable.append(
                {
                    "instance_id": instance["id"],
                    "name": instance["name"],
                    "missing_symbols": sorted(set(missing) | {r["symbol"] for r in unavailable}),
                    "evaluated_rules": [r.rule_id for r in verdict.results if r.fires is not None],
                    "unavailable_rules": unavailable,
                }
            )
        if old_required and previous["decision"] == escalate:
            # A past escalation for these same fields already applied the policy.
            reason = (await session.get(Decision, previous["id"])).reason
            code, _, names = reason.partition(" | ")[0].partition(": ")
            if code in ("MISSING_DATA", "UNVERIFIED_DATA") and old_required <= set(
                names.split(", ")
            ):
                already_escalated.append(
                    {"instance_id": instance["id"], "name": instance["name"], "reason": reason}
                )
                old_required = set()
                if not real_failures:
                    continue
        if real_failures or old_required:
            details = [
                *real_failures,
                *(
                    ["MISSING_EXISTING_REQUIRED: " + ", ".join(sorted(old_required))]
                    if old_required
                    else []
                ),
            ]
            errors.append({"instance_id": instance["id"], "reason": " | ".join(details)})
            continue
        if missing or unavailable:
            continue
        if previous["decision"] == verdict.decision:
            unchanged += 1
            continue
        change = {
            "instance_id": instance["id"],
            "decision_id": previous["id"],
            "name": instance["name"],
            "before": previous["decision"],
            "after": verdict.decision,
            "reason": verdict.reason,
        }
        if latest["author"] != "engine":
            # Information only: the person's decision stays as it is.
            resolved_by_person.append(
                {**change, "resolution": latest["decision"], "resolved_by": latest["author"]}
            )
        elif previous["pending_review"]:
            conflicts.append(change)
        else:
            changes.append(change)
    if baseline and newly_required:
        if tables is None:
            from app.features.sources.model import Source

            rows = await session.scalars(select(Source).where(Source.id.in_(inputs["source_ids"])))
            tables = {row.name: row.rows for row in rows}
        examples = await _check_schema_examples(snapshot, newly_required, tables)
    else:
        examples = {"valid": True, "tested": 0}
    return {
        "valid": not conflicts and not errors and examples["valid"],
        "unchanged": unchanged,
        "changes": changes,
        "conflicts": conflicts,
        "errors": errors,
        "example_validation": examples,
        "not_evaluable": not_evaluable,
        "already_escalated": already_escalated,
        "resolved_by_person": resolved_by_person,
        "coverage": {
            "total": len(selected),
            "evaluated": len(selected) - len(not_evaluable),
            "not_evaluable": len(not_evaluable),
            "partial": sum(bool(row["evaluated_rules"]) for row in not_evaluable),
            "none": sum(not row["evaluated_rules"] for row in not_evaluable),
        },
    }


async def validate(session, process_id: int, author: str | None = None) -> ProcessDraft:
    process = await lock(session, process_id)
    draft = await get_draft(session, process_id)
    if draft.base_version_id != process.active_version_id:
        raise ConflictError("The active version changed; rebase the draft before validation")
    inputs = await execution.capture(session, process_id)
    # The replay's `evaluate_rule` spans hang under this one, never under a real run.
    with events.span("validate_process_draft", process_id=process_id, author=author) as span:
        report = await inspect(session, draft.snapshot, inputs)
        span.set(
            revision=draft.revision,
            valid=report["valid"],
            **{k: len(report.get(k, [])) for k in ("changes", "conflicts", "errors")},
        )
        if not report["valid"]:
            span.status = "error"
            span.data["error"] = report.get("error") or "validation found conflicts or errors"
    draft.validation = {
        **report,
        "inputs_hash": config.digest(inputs),
        "snapshot_hash": config.digest(draft.snapshot),
        "revision": draft.revision,
    }
    draft.validation = {**draft.validation, "hash": config.digest(draft.validation)}
    await session.commit()
    return draft


async def publish_snapshot(
    session, process: Process, snapshot: dict, author: str, reason: str, report: dict
) -> ProcessVersion:
    """Caller holds the process lock and has validated this exact candidate and evidence."""
    previous = await active(session, process.id, required=False)
    row = ProcessVersion(
        process_id=process.id,
        number=previous.number + 1 if previous else 1,
        parent_id=previous.id if previous else None,
        snapshot=deepcopy(snapshot),
        content_hash=config.digest(snapshot),
        validation=deepcopy(report),
        author=author,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    process.active_version_id = row.id
    process.use_case_id = snapshot["process"]["use_case_id"]
    selected = {r["id"]: r for r in snapshot["rules"]}
    for rule in await session.scalars(select(Rule).where(Rule.process_id == process.id)):
        if rule.id in selected:
            for key in config.RULE_FIELDS:
                if key not in ("id", "status"):
                    setattr(rule, key, deepcopy(selected[rule.id][key]))
            rule.status = selected[rule.id]["status"]
            rule.activated_at = row.created_at
        elif rule.status in ("active", "blocked"):
            rule.status = "retired"
    from app.features.decisions.model import Decision

    for change in report.get("changes", []):
        decision = await session.get(Decision, change["decision_id"])
        historical = (
            await session.get(ProcessVersion, decision.version_id)
            if decision.version_id
            else previous
        )
        human = (
            {
                t["name"]
                for t in historical.snapshot["process"]["decision_types"]
                if t["requires_human"]
            }
            if historical
            else set()
        )
        if change["before"] in human:
            continue
        session.add(
            Finding(
                decision_id=change["decision_id"],
                rule_id=None,
                type="different_decision",
                detail=(
                    f"Process version {row.number}: "
                    f"{change['before']} -> {change['after']}: {change['reason']}"
                ),
            )
        )
    events.record(
        session,
        "publish_process_version",
        process_id=process.id,
        data={"version_id": row.id, "parent_id": row.parent_id, "author": author, "reason": reason},
    )
    # A rule suggested for the previous version amends a rule that may have changed.
    await supersede(
        session,
        "version_published",
        ManagerProposal.process_id == process.id,
        ManagerProposal.channel == "escalation",
        ManagerProposal.kind == "rule",
    )
    return row


async def publish(
    session, process_id: int, revision: int, validation_hash: str, author: str, reason: str
) -> ProcessVersion:
    process = await lock(session, process_id)
    draft = await get_draft(session, process_id)
    validation = draft.validation or {}
    refused = None
    if (
        draft.revision != revision
        or validation.get("hash") != validation_hash
        or not validation.get("valid")
    ):
        refused = "Approve the latest successful validation of this exact draft revision"
    elif (
        process.active_version_id != draft.base_version_id
        or validation["snapshot_hash"] != config.digest(draft.snapshot)
        or validation["inputs_hash"] != config.digest(await execution.capture(session, process_id))
    ):
        refused = "Configuration or execution evidence changed; validate again"
    if refused:  # a refused publication is an `error` span: who tried, on which revision
        with events.span(
            "publish_process_version", process_id=process_id, author=author, revision=revision
        ) as span:
            span.status = "error"
            span.data["error"] = refused
        raise ConflictError(refused)
    row = await publish_snapshot(session, process, draft.snapshot, author, reason, validation)
    await session.delete(draft)
    await session.commit()
    from app.features.alerts.service import after_publish

    await after_publish(process_id, row.id)
    return row
