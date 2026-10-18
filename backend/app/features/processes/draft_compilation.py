"""Compile reviewed rules without activating them; reuse the engine and historical audit."""

import asyncio
from dataclasses import replace

from app.common.exceptions import ConflictError
from app.features.agents import compiler, normalizer, sandbox
from app.features.decisions import service as decisions
from app.features.decisions.engine import Outcomes, decide
from app.features.processes.draft_schemas import DraftPlan
from app.features.processes.model import DecisionType, Symbol
from app.features.rules.model import Rule
from app.features.rules.service import rule_hash
from app.features.versions import configuration as config
from app.features.versions import execution
from app.features.versions import service as versions


def proposals(plan: DraftPlan) -> list[str]:
    return [
        "setup",
        *[f"connector:{c.name}" for c in plan.connectors],
        *[f"source:{s.name}" for s in plan.sources],
        *[f"rule:{r.name}" for r in plan.rules],
        *[f"guidance:{g.name}" for g in plan.guidance],
        *[f"example:{e.name}" for e in plan.examples],
    ]


def source_fields(plan: DraftPlan) -> dict[str, set[str]]:
    """The field names each proposed source will actually carry. A snapshot's rows come from
    a connector, so the plan alone does not say."""
    known = {}
    for source in plan.sources:
        if source.kind == "workbook":
            known[source.name] = set(source.columns)
        elif source.kind == "constant":
            known[source.name] = {key for row in source.rows for key in row}
    return known


def consistent_examples(plan: DraftPlan) -> None:
    """An example's rows replace the real table for that check, so a row written with the
    spreadsheet's own header instead of the name the source maps breaks every rule reading
    that table. Without this the mismatch only shows up as sandbox errors, after compiling
    and testing every rule."""
    known = source_fields(plan)
    proposed = {s.name for s in plan.sources}
    for example in plan.examples:
        for table, rows in example.sources.items():
            if table not in proposed:
                raise ConflictError(
                    f"Example `{example.name}` supplies rows for `{table}`, which is not a "
                    f"proposed source ({', '.join(sorted(proposed)) or 'none proposed'})"
                )
            unknown = {key for row in rows for key in row} - known.get(table, set())
            if table in known and unknown:
                raise ConflictError(
                    f"Example `{example.name}` gives `{table}` the field(s) "
                    f"{', '.join(sorted(unknown))}, which its source does not map. Rules read "
                    f"{', '.join(sorted(known[table]))}; write the example with those names"
                )


def usable_extraction(plan: DraftPlan) -> None:
    """A symbol's `extraction.source` says where its value comes from: `document` (default,
    the labels are matched in the page), `filename`, `text` (the WHOLE transcript, for one
    free-text symbol) or `none`. Asking for the whole transcript while declaring labels, or
    for anything that is not text, produces a symbol that can only be the entire page or
    null. Observed with the hiring pack: every symbol was published as `text`, so each case
    escalated on data the reader had in front of it."""
    for symbol in plan.symbols:
        extraction = symbol.extraction
        if extraction is None or extraction.source != "text":
            continue
        if symbol.type not in {"text", "string"}:
            raise ConflictError(
                f"Symbol `{symbol.name}` is a {symbol.type} but reads the whole transcript "
                '(extraction.source "text"), which can only be null. Use "document" to match '
                "its labels in the page"
            )
        if extraction.labels:
            raise ConflictError(
                f"Symbol `{symbol.name}` declares labels but reads the whole transcript "
                '(extraction.source "text"), so the labels are ignored and its value is the '
                'entire page. Use "document" to match them'
            )


def ready(plan: DraftPlan, reviews: dict):
    if plan.questions:
        raise ConflictError("Answer the outstanding questions before compiling")
    if not plan.name.strip() or not plan.examples:
        raise ConflictError("A name and confirmed acceptance examples are required")
    if any(reviews.get(p) != "accepted" for p in proposals(plan)):
        raise ConflictError("Review and accept every proposal before compiling")
    try:
        plan.definition()
    except ValueError as error:
        raise ConflictError(str(error)) from error
    known = {t.name for t in plan.decision_types}
    if any(e.decision not in known for e in plan.examples):
        raise ConflictError("An acceptance example names an unknown outcome")
    consistent_examples(plan)
    usable_extraction(plan)


def compiled_rules(compilations: list[dict]) -> list[Rule]:
    return [
        Rule(
            id=r.get("existing_rule_id", -(i + 1)),
            process_id=0,
            norm_rule_id=r.get("norm_rule_id"),
            text=r["text"],
            summary=r.get("summary"),
            type=r["type"],
            decision=r["decision"],
            code=r["code"],
            hash=r["hash"],
            report=r["report"],
            tests=r.get("tests", []),
            status="active",
        )
        for i, r in enumerate(compilations)
    ]


def candidate(plan: DraftPlan, compilations: list[dict], base: dict) -> dict:
    from copy import deepcopy

    snapshot = deepcopy(base)
    snapshot["rules"] = [config.artifact(r) for r in compiled_rules(compilations)]
    snapshot["process"].update(
        plan.model_dump(
            include={"name", "description", "decision_types", "symbols", "decision_review"}
        )
    )
    snapshot["guidance"] = {g.name: g.text for g in plan.guidance}
    snapshot["acceptance_examples"] = [e.model_dump() for e in plan.examples]
    snapshot["connectors"] = {
        connector.name: connector.config.model_dump(mode="json") for connector in plan.connectors
    }
    snapshot["source_schemas"] = {
        connector.name: {
            "required": connector.required,
            "optional": connector.optional,
            "sync_before_run": connector.sync_before_run,
        }
        for connector in plan.connectors
    }
    return snapshot


async def compile_plan(
    plan: DraftPlan,
    tables: dict,
    setups: dict,
    base: dict | None = None,
    base_tables: dict | None = None,
) -> list[dict]:
    symbols = [Symbol(process_id=0, **s.model_dump()) for s in plan.symbols]
    types = [DecisionType(process_id=0, **t.model_dump()) for t in plan.decision_types]
    compilations = []
    context_unchanged = (
        base is not None
        and tables == base_tables
        and all(
            base["process"][key] == plan.model_dump()[key]
            for key in ("description", "symbols", "decision_types")
        )
    )
    for proposal in plan.rules:
        existing = next(
            (
                r
                for r in (base or {}).get("rules", [])
                if proposal.name == f"rule-{r['id']}"
                and all(r[key] == getattr(proposal, key) for key in ("text", "type", "decision"))
            ),
            None,
        )
        if context_unchanged and existing and (existing["report"] or {}).get("valid"):
            compilations.append(
                {
                    **existing,
                    "proposal": proposal.name,
                    "existing_rule_id": existing["id"],
                    "interpretation": "Unchanged published rule",
                }
            )
            continue
        # The manager's chosen outcome is explicit. Never inherit a use-case rejection
        # default over the outcome the user just approved.
        setup = setups.get("normalizer")
        if setup:
            setup = replace(
                setup, settings=setup.settings.model_copy(update={"failed_check_decision": None})
            )
        text = (
            f"{proposal.text}\nRule type: {proposal.type}. "
            f"When this rule fires, outcome: {proposal.decision}."
        )
        normalized, _ = await normalizer.normalize(
            text, plan.description, types, symbols, tables, [], setup
        )
        checks = [c for s in normalized.norm_rules for c in s.checks]
        if not checks or any(c.decision != proposal.decision for c in checks):
            raise ConflictError(
                f"Normalizer changed the approved outcome for {proposal.name}; clarify its rule"
            )
        for check in checks:
            rule = Rule(text=check.text, type=check.type, decision=proposal.decision, process_id=0)
            # The manager approved the proposal's line; a split proposal keeps each check's own.
            summary = (proposal.summary if len(checks) == 1 else "") or check.summary
            result = await compiler.compile_text(
                rule, symbols, tables, plan.description, compiler.Runs(setups)
            )
            compilations.append(
                {
                    "proposal": proposal.name,
                    "text": rule.text,
                    "summary": summary or None,
                    "type": rule.type,
                    "decision": rule.decision,
                    "code": result.code,
                    "tests": result.tests,
                    "hash": rule_hash(rule.text, result.code) if result.code else None,
                    "report": result.report,
                    "interpretation": check.interpretation,
                }
            )
    return compilations


async def preview(
    session,
    process_id: int | None,
    plan: DraftPlan,
    tables: dict,
    compilations: list[dict],
    base: dict | None = None,
) -> dict:
    rules = compiled_rules(compilations)
    types = plan.decision_types
    outcomes = Outcomes(
        {t.name: t.priority for t in types},
        next(t.name for t in types if t.is_default),
        max((t for t in types if t.requires_human), key=lambda t: t.priority).name,
        tuple(s.name for s in plan.symbols if s.required),
    )
    results = []
    for example in plan.examples:
        dataset = [(0, example.instance)]
        population = [*dataset, *((i + 1, row) for i, row in enumerate(example.others))]
        verdict = (
            await asyncio.to_thread(
                decide,
                rules,
                outcomes,
                dataset,
                {**tables, **example.sources},
                population,
                sandbox.run_dataset,
            )
        )[0]
        results.append(
            {
                "name": example.name,
                "expected": example.decision,
                "actual": verdict.decision,
                "reason": verdict.reason,
                "passed": verdict.decision == example.decision
                and not verdict.reason.startswith(
                    ("RULE_ERROR", "RULE_NEEDS_DATA", "RULE_CONFLICT")
                ),
            }
        )
    inputs = await execution.capture(session, process_id) if process_id else None
    if base is None:
        base = (
            (await versions.active(session, process_id)).snapshot
            if process_id
            else {
                "process": {"id": 0, "use_case_id": 0},
                "agents": {},
                "guidance": {},
            }
        )
    proposed = candidate(plan, compilations, base)
    impact = {"valid": True, "unchanged": 0, "changes": [], "conflicts": [], "errors": []}
    if process_id:
        impact = await versions.inspect(session, proposed, inputs, tables=tables)
    else:
        try:
            await asyncio.to_thread(versions.check_configuration, proposed)
        except (ValueError, sandbox.SandboxError) as error:
            impact = {**impact, "valid": False, "error": str(error)}
    review = None
    from app.features.sources import service as sources

    source_changed = process_id is not None and tables != {
        source.name: source.rows for source in await sources.current_loads(session, process_id)
    }
    if process_id and impact["valid"] and review_changed(base, proposed, source_changed):
        review = await review_preview(
            session,
            process_id,
            base,
            proposed,
            inputs,
            tables,
            excluded_ids={row["instance_id"] for row in impact["not_evaluable"]},
        )
    return {
        "valid": all(r["report"].get("valid") for r in compilations)
        and all(e["passed"] for e in results)
        and impact["valid"]
        and (review is None or review["valid"]),
        "review": review,
        "compilations": compilations,
        "examples": results,
        "examples_basis": (
            "Acceptance examples test supplied values, not document ingestion or extraction."
        ),
        "extraction_warnings": [
            {
                "code": "REQUIRED_INPUT_EXTRACTION_DISABLED",
                "symbol": symbol.name,
                "source": "none",
                "message": (
                    f"Required input `{symbol.name}` is not read from uploaded documents. "
                    "Configure document extraction or confirm how it is supplied externally."
                ),
            }
            for symbol in plan.symbols
            if symbol.required and symbol.extraction and symbol.extraction.source == "none"
        ],
        "impact": impact,
        "source_counts": {name: len(rows) for name, rows in tables.items()},
        "replaced_rules": [
            {"id": r.id, "text": r.text, "decision": r.decision}
            for r in (await decisions.active_rules(session, process_id) if process_id else [])
        ],
    }


def review_changed(before: dict, after: dict, source_changed: bool = False) -> bool:
    enabled = before["process"].get("decision_review") or after["process"].get("decision_review")
    return bool(enabled) and (
        source_changed
        or any(before.get(key) != after.get(key) for key in ("process", "guidance", "rules"))
    )


async def review_preview(session, process_id, before, after, inputs, tables, *, excluded_ids=None):
    from copy import deepcopy
    from dataclasses import asdict

    from app.features.learning import evidence, validation

    captured = await evidence.capture(session, process_id)
    eligible = {
        **captured,
        "instances": [
            case for case in captured["instances"] if case["id"] not in (excluded_ids or set())
        ],
    }
    sampled = evidence.cases(eligible, 10)
    if not sampled:
        return {
            "valid": True,
            "previews": [],
            "basis": "No historical cases with complete proposed symbols available",
        }
    snapshots = []
    for configuration, source_tables in ((before, None), (after, tables)):
        snapshot = deepcopy(eligible)
        snapshot["execution"] = deepcopy(configuration.get("execution", {}))
        for key in ("process", "rules", "guidance", "agents"):
            snapshot[key] = deepcopy(configuration[key])
        if source_tables is not None:
            snapshot["sources"] = [
                {"id": f"proposed:{name}", "name": name, "rows": rows, "origin": "draft"}
                for name, rows in source_tables.items()
            ]
        verdicts = await execution.evaluate(
            session,
            configuration,
            inputs,
            [i["id"] for i in sampled],
            tables=source_tables,
        )
        for decision in snapshot["decisions"]:
            if decision["author"] == "engine" and decision["instance_id"] in verdicts:
                result = verdicts[decision["instance_id"]]
                decision.update(
                    decision=result.decision,
                    reason=result.reason,
                    results=[asdict(r) for r in result.results],
                    rules_hash=result.rules_hash,
                )
        snapshot["prompts"]["decision_reviewer"] = (
            configuration.get("reviewer_prompt") or snapshot["prompts"]["decision_reviewer"]
        )
        snapshots.append(snapshot)
    try:
        result = await validation.compare_reviews(*snapshots)
    except TimeoutError as error:
        from app.features.agents.llm import AgentError

        raise AgentError("Reviewer preview timed out; the draft was not changed") from error
    originals = {case["id"]: case for case in sampled}
    for preview in result["previews"]:
        preview["final_decision"] = originals[preview["instance_id"]]["decisions"][-1]
    result["basis"] = (
        "Published and proposed configurations evaluated on captured current evidence; "
        "cases without newly required symbols were excluded; paired reviewer "
        "recommendations can vary and do not change past decisions"
    )
    return result
