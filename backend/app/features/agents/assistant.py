"""Escalation assistant: suggests a decision, its reasoning and a new rule; after a person
resolved the case, `suggest_rule` amends the escalation rule that fired (ADR 0035).

The LLM never decides anything in the pipeline (ADR 0002). This is only a suggestion shown
to a person, who then resolves the case and, if they want, adds the proposed rule (which is
compiled and validated like any other rule)."""

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, NotFoundError
from app.core import events
from app.features.agents import llm
from app.features.decisions.model import ENGINE, Decision
from app.features.ingestion.model import File, Instance
from app.features.ingestion.symbols import flatten_symbols
from app.features.processes.model import DecisionType, Process
from app.features.proposals.model import ManagerProposal
from app.features.rules.model import Rule
from app.features.use_cases import service as use_cases
from app.features.use_cases.model import UseCase

MAX_TEXT = 4_000  # chars of the file's text sent to the model: the symbols carry the data
MAX_RESOLUTIONS = 10


# Hard limits: the manager reads this on a queue, so no essays. A Spanish text over its
# limit is trimmed in place (`tidy`), never sent back: every retry is a whole extra model
# call. A rule is compiled, so a rule over RULE is an error the model fixes.
LINE = 180  # one line
SHORT = 320  # at most two short sentences
RULE = 600  # one rule, in English (the longest rule of the invoice pack is 423)


def cut(most: int):
    """Over `most` characters, cut at the last whole word and end with an ellipsis."""

    def check(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= most else text[: most - 1].rsplit(" ", 1)[0] + "…"

    return AfterValidator(check)


Line = Annotated[str, cut(LINE)]
Short = Annotated[str, cut(SHORT)]


class Option(BaseModel):
    decision: str
    # one line in Spanish: what happens and under which rule ("Se paga si se añade la regla:
    # ...; casos como este pasarían a PAGAR", "No se paga por la regla 7: ...")
    consequence: Line
    # English: the rule that justifies this decision, new or an amendment of the escalating
    # one; or "Rule <id>" for an active rule that already gives it. None only on no_rule_reason
    rule: str | None = Field(default=None, max_length=RULE)


class Suggestion(BaseModel):
    decision: str  # the proposed one of `options`
    reasoning: Short
    why: list[Line] = Field(min_length=1, max_length=2)
    options: list[Option] = Field(min_length=1)  # every final decision type, one each
    evidence: list[str] = Field(min_length=1)  # references from `evidence_refs`, six kept
    # rule text, to be compiled like any other rule if accepted; None with `no_rule_reason`
    proposed_rule: str | None = Field(default=None, max_length=RULE)
    proposed_type: Literal["requirement", "prohibition"] | None = None
    # in Spanish: why no rule should decide cases like this one (a person must always look)
    no_rule_reason: Short | None = None


RULE_CODE = re.compile(r"\b(?:([Ll]a) )?(?:regla )?R0*(\d{1,3})\b")


def tidy(text: str | None, most: int) -> str | None:
    """A Spanish text fixed in place: one line, its first `most` sentences, no backticks,
    and a rule code like R09 said as «la regla 9»."""
    if not text:
        return text
    text = " ".join(text.replace("`", "").split())
    text = RULE_CODE.sub(lambda m: f"{m[1] or 'la'} regla {m[2]}", text)
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", text)]
    return text[: ends[most - 1]] if len(ends) > most else text


def cited(evidence: list[str], references: set[str]) -> list[str]:
    """The evidence the model may cite, at most six; a made-up reference is dropped. Nothing
    left is a real error: ModelRetry."""
    kept = [e for e in evidence if e in references][:6]
    if not kept:
        raise ModelRetry(f"cite only references in evidence_refs: {sorted(set(evidence))}")
    return kept


# What a manager should never read: field names, rule codes, reference ids.
JARGON = re.compile(
    r"`|\bR\d{1,2}\b|\b(?:document|symbol|rule|resolution):|\b\w+_\w+\b|\b[a-z]+\.[a-z_]+\b"
)
# File names are fine for a manager (they name the invoice), even with an underscore.
FILE_NAME = re.compile(r"\S+\.(?:pdf|png|jpe?g|tiff?|xml)\b", re.IGNORECASE)
# The policy: an escalated case is decided by a person, never closed by the advice.
NO_HUMAN = re.compile(
    r"sin (?:intervención|revisión|supervisión) humana|se cierra sin"
    r"|no (?:hace falta|es necesario|necesita)[^.;]{0,40}(?:persona|revis|humano)",
    re.IGNORECASE,
)


def plain(allowed: set[str], **texts: str | None) -> None:
    """ModelRetry naming the Spanish texts written with jargon or that skip the person.
    `allowed`: words that are fine as they are (decision type names)."""
    wrong = []
    for name, text in texts.items():
        words = sorted(allowed, key=len, reverse=True)
        text = re.sub("|".join(map(re.escape, words)) or "$^", "", text or "")
        text = FILE_NAME.sub("", text)
        if found := JARGON.findall(text):
            wrong.append(f"{name} uses {sorted(set(found))}")
        if NO_HUMAN.search(text):
            wrong.append(f"{name} says the case closes without a person")
    if wrong:
        raise ModelRetry(
            "Write for a manager in plain Spanish: no field names (use their Spanish label), "
            "no rule codes like R09 (say «la regla 9» or what it checks), no references, and "
            "never that the case closes without a person: " + "; ".join(wrong)
        )


# Escalations the engine made by itself (data or infrastructure, not a rule): a person
# must decide them, so the advice always offers to keep the case escalated.
ENGINE_CODES = {
    "MISSING_DATA",
    "UNVERIFIED_DATA",
    "SOURCE_UNAVAILABLE",
    "RULE_CONFLICT",
    "SCAN_REVIEW",
    "RULE_ERROR",
    "RULE_NEEDS_DATA",
    "RULE_COMPILE_FAILED",
}


def engine_code(reason: str | None) -> str:
    head = re.split(r"[:\s]", (reason or "").split(" | ", 1)[0].strip(), maxsplit=1)[0]
    return head if head in ENGINE_CODES else ""


@dataclass(frozen=True)
class Deps:
    decision_types: list[str]
    final_types: list[str]  # the ones that close a case: every option
    references: set[str]
    engine_code: str = ""  # the engine escalated by itself (MISSING_DATA...): a person decides
    related: tuple[str, ...] = ()  # the other cases the fired rules name (a duplicate order)
    first: str = ""  # which of this case and `related` came first (`arrival`)
    this: str = ""  # this case's name
    paid: str = ""  # the default decision type: what paying means (PAGAR)


# Its platform prompt is `prompts/assistant.md`; the use case adds guidance and model.
# One retry at most: trivial problems are fixed in place, and each retry is a whole call.
assistant = Agent(None, output_type=Suggestion, deps_type=Deps, name="assistant", retries=1)


@assistant.output_validator
def _known_decision(ctx: RunContext[Deps], suggestion: Suggestion) -> Suggestion:
    deps = ctx.deps
    # trivial problems are fixed here, not sent back
    suggestion.reasoning = tidy(suggestion.reasoning, 2)
    suggestion.no_rule_reason = tidy(suggestion.no_rule_reason, 2)
    suggestion.why = [tidy(w, 1) for w in suggestion.why]
    for option in suggestion.options:
        option.consequence = tidy(option.consequence, 1)
    suggestion.evidence = cited(suggestion.evidence, deps.references)
    consistent_pair(deps, suggestion)
    if suggestion.no_rule_reason:  # "no rule" wins over a rule given with it
        suggestion.proposed_rule = suggestion.proposed_type = None
    if suggestion.decision not in deps.decision_types:
        raise ModelRetry(f"decision {suggestion.decision!r} is not one of {deps.decision_types}")
    human = [t for t in deps.decision_types if t not in deps.final_types][:1]
    expected = deps.final_types + (human if deps.engine_code else [])
    # an extra option (keeping it escalated when the engine did not escalate by itself) goes
    suggestion.options = [o for o in suggestion.options if o.decision in expected]
    options = [o.decision for o in suggestion.options]
    if sorted(options) != sorted(expected):
        keep = (
            f" ({human[0]}: 'mantener escalado / pedir el dato'; {deps.engine_code} is an "
            "engine escalation a person must decide)"
            if deps.engine_code
            else ""
        )
        raise ModelRetry(
            f"options must be exactly one each, for: {', '.join(expected)}{keep}; you gave "
            f"{options}"
        )
    if suggestion.decision not in options:
        raise ModelRetry(
            f"decision must be one of option_types {expected}, also with no_rule_reason: "
            "that a person must always decide goes in no_rule_reason, not in decision"
        )
    if deps.engine_code and not suggestion.no_rule_reason:
        raise ModelRetry(
            f"{deps.engine_code} is an engine escalation, not a rule: no rule decides it. "
            "Give no_rule_reason saying a person must decide (and what to obtain)"
        )
    if deps.related and not suggestion.no_rule_reason:
        said = " ".join([suggestion.reasoning, *(o.consequence for o in suggestion.options)])
        if not any(n.rsplit(".", 1)[0].casefold() in said.casefold() for n in deps.related):
            raise ModelRetry(
                f"the fired rule involves other cases ({', '.join(deps.related)}): advise "
                "both consistently, saying which one is paid and which one is not (for "
                "example the first received is paid, the duplicate is not), or answer "
                "no_rule_reason. Rejecting all of them means the order is never paid"
            )
    if not suggestion.no_rule_reason:
        if not (suggestion.proposed_rule and suggestion.proposed_type):
            raise ModelRetry("give proposed_rule and proposed_type, or no_rule_reason")
        if missing := [o.decision for o in suggestion.options if not o.rule]:
            raise ModelRetry(f"every option needs the rule that justifies it: {missing}")
    plain(
        {*deps.decision_types, *deps.related, *(n.rsplit(".", 1)[0] for n in deps.related)},
        reasoning=suggestion.reasoning,
        no_rule_reason=suggestion.no_rule_reason,
        **{f"why[{i}]": w for i, w in enumerate(suggestion.why)},
        **{f"{o.decision}.consequence": o.consequence for o in suggestion.options},
    )
    return suggestion


PAID = r"(?<!no )\bse (?:paga|pagaría|pagará|debe pagar|debería pagar)\b"


def consistent_pair(deps: Deps, suggestion: Suggestion) -> None:
    """A duplicate pair advised against the facts: `first` is computed, never the model's.
    Only `first` may be called the first received, and the decision must agree with which
    one the text pays: the other one paid means this one is not, and the reverse."""
    if not deps.related or not deps.first:
        return
    said = " ".join(
        [suggestion.reasoning, *suggestion.why, *(o.consequence for o in suggestion.options)]
    )
    stems = {n: n.rsplit(".", 1)[0].casefold() for n in (deps.this, *deps.related)}

    def named(text: str) -> set[str]:  # "esta" is this case, unless "esta segunda"
        found = {
            n
            for n, stem in stems.items()
            if re.search(rf"(?<![\w-]){re.escape(stem)}(?![\w-])", text)
        }
        return found | ({deps.this} if re.search(r"\best[ae]\b(?! segund)", text) else set())

    wrong = [
        m[0]
        for m in re.finditer(r"[^.;]*?\bprimer\w*[^;]*?(?= y |;|\.\s|$)", said.casefold())
        if (who := named(m[0])) and deps.first not in who
    ]
    if wrong:
        raise ModelRetry(
            f"the first received is {deps.first} (escalation.first_received), not what you "
            f"say in: {' | '.join(w.strip() for w in wrong)}"
        )
    for clause in re.split(r"[;]|\.\s", suggestion.reasoning.casefold()):
        paid = re.search(PAID + r"(?:(?! y ).)*", clause)  # up to "y la otra no"
        who = named(paid[0]) if paid and deps.paid else set()
        if len(who) != 1:
            continue
        if deps.this in who and suggestion.decision != deps.paid:
            raise ModelRetry(f"you say this case is paid, so decision must be {deps.paid}")
        if deps.this not in who and suggestion.decision == deps.paid:
            raise ModelRetry(
                f"you say {who.pop()} is the one paid, so this case is not: decision cannot "
                f"be {deps.paid}"
            )


def arrival(instance: Instance) -> tuple:
    """Which of two cases came first: the document's `date` (ISO), then the order they were
    received in. A case without a date goes after the dated ones."""
    date = str(flatten_symbols(instance.symbols or {}).get("date") or "")
    return (not date, date, instance.id)


async def related_cases(session: AsyncSession, instance: Instance, evidence: str) -> list[dict]:
    """The other cases `evidence` names (a duplicate order names the invoices it shares the
    order with): their symbols, which of them equal this case's, their current decision and
    whether they came before or after this one (`arrival`)."""
    from app.features.decisions.service import latest_decisions

    others = list(
        await session.scalars(
            select(Instance)
            .where(
                Instance.process_id == instance.process_id,
                Instance.id != instance.id,
                func.strpos(literal(evidence or ""), Instance.name) > 0,
            )
            .order_by(Instance.id)
            .limit(MAX_RESOLUTIONS)
        )
    )
    # a whole name only: "c" is inside "purchase"
    others = [
        o for o in others if re.search(rf"(?<![\w.-]){re.escape(o.name)}(?![\w.-])", evidence)
    ]
    latest = await latest_decisions(session, others)
    case = flatten_symbols(instance.symbols or {})
    related = []
    for other in others:
        flat = flatten_symbols(other.symbols or {})
        related.append(
            {
                "name": other.name,
                "symbols": flat,
                "same_as_case": sorted(k for k in case if k in flat and flat[k] == case[k]),
                "decision": latest[other.id].decision if other.id in latest else None,
                "received": "before" if arrival(other) < arrival(instance) else "after",
            }
        )
    return related


async def _context(session: AsyncSession, instance: Instance) -> tuple[dict[str, Any], list[str]]:
    latest = await session.scalar(
        select(Decision)
        .where(Decision.instance_id == instance.id)
        .order_by(Decision.id.desc())
        .limit(1)
    )
    pid = instance.process_id
    all_types = list(
        await session.scalars(
            select(DecisionType)
            .where(DecisionType.process_id == pid)
            .order_by(DecisionType.priority.desc())
        )
    )
    from app.features.processes.schemas import DecisionTypeIO
    from app.features.versions.configuration import rules as frozen_rules
    from app.features.versions.model import ProcessVersion

    version = (
        await session.get(ProcessVersion, latest.version_id)
        if latest and latest.version_id
        else None
    )
    if version:
        all_types = [
            DecisionTypeIO.model_validate(t) for t in version.snapshot["process"]["decision_types"]
        ]
    types = [t.name for t in all_types]
    human = [t.name for t in all_types if t.requires_human]
    if not (latest and latest.decision in human):
        raise ConflictError(f"Instance {instance.id} is not escalated")

    rules = {r.id: r for r in await session.scalars(select(Rule).where(Rule.process_id == pid))}
    if version:
        rules = {r.id: r for r in frozen_rules(version.snapshot)}
    file = await session.get(File, instance.file_hash)
    resolutions = await session.execute(
        select(Decision, Instance.symbols)
        .join(Instance, Decision.instance_id == Instance.id)
        .where(Instance.process_id == pid, Decision.author != ENGINE)
        .order_by(Decision.id.desc())
        .limit(MAX_RESOLUTIONS)
    )

    process = await session.get(Process, pid)
    use_case = await session.get(UseCase, process.use_case_id)
    fired = [r for r in latest.results if r.get("fires")]
    context = {
        # conventions every rule of the process follows; the proposed rule must too
        "version_id": version.id if version else None,
        "use_case_description": version.snapshot["process"]["description"]
        if version
        else use_case.description,
        "decision_types": types,
        "human_decision_types": human,
        "case": {
            "name": instance.name,
            "symbols": flatten_symbols(instance.symbols or {}),
            "file_text": ((file.text if file else None) or "")[:MAX_TEXT],
        },
        # what each symbol means, to name it in plain Spanish instead of by its name
        "symbol_meanings": {
            s["name"]: s.get("description", "")
            for s in (version.snapshot["process"]["symbols"] if version else [])
        },
        "escalation": {
            "current_decision": latest.decision,
            "reason": latest.reason,
            "fired_rules": [
                {
                    "id": r.get("rule_id"),
                    "reason": r.get("reason"),
                    "text": rules[r["rule_id"]].text if r.get("rule_id") in rules else None,
                    "decision": rules[r["rule_id"]].decision if r.get("rule_id") in rules else None,
                }
                for r in fired
            ],
            # the engine escalated by itself (missing data, a source down...): no rule
            "engine_code": engine_code(latest.reason) or None,
            # other cases the fired rules name (a duplicate order): advise them consistently
            "related_cases": await related_cases(
                session,
                instance,
                " ".join([latest.reason or "", *(r.get("reason") or "" for r in fired)]),
            ),
        },
        "active_rules": [
            {"id": r.id, "text": r.text, "type": r.type, "decision": r.decision}
            for r in rules.values()
            if r.status == "active"
        ],
        "human_resolutions": [
            {
                "ref": f"resolution:{d.id}",
                "symbols": flatten_symbols(symbols or {}),
                "decision": d.decision,
                "reason": d.reason,
            }
            for d, symbols in resolutions
        ],
    }
    related = context["escalation"]["related_cases"]
    if related:  # a fact, by name: the model misread a per-case "before"/"after"
        before = [r["name"] for r in related if r["received"] == "before"]
        context["escalation"]["first_received"] = before[0] if before else instance.name
    # the options to answer, one each: the final types, and keeping it escalated when the
    # engine escalated by itself
    context["paid"] = next((t.name for t in all_types if t.is_default), "")
    context["option_types"] = [t for t in types if t not in human] + (
        human[:1] if context["escalation"]["engine_code"] else []
    )
    context["evidence_refs"] = sorted(
        {f"symbol:{name}" for name in instance.symbols or {}}
        | {f"rule:{r['id']}" for r in context["escalation"]["fired_rules"] if r["id"]}
        | {r["ref"] for r in context["human_resolutions"]}
        | ({"file"} if context["case"]["file_text"] else set())
        | {"escalation"}
    )
    return context, types


async def _current_assistant_setup(
    session: AsyncSession, process_id: int, context_version_id: int | None
) -> llm.Setup | None:
    """New advice uses the currently approved assistant, not a retired historical provider.

    The calling workflow keeps the decision's original rules, outcomes and evidence.
    This is a new model call, never deterministic replay or an edit to the old snapshot.
    """
    from app.features.versions.configuration import setups
    from app.features.versions.service import active

    version = await active(session, process_id, required=False)
    if version:
        configured = setups(version.snapshot)
    else:
        process = await session.get(Process, process_id)
        configured = await use_cases.setups(session, process.use_case_id)
    if span := events.current():
        span.set(
            context_version_id=context_version_id,
            model_version_id=version.id if version else None,
        )
    return configured.get("assistant")


async def suggest(session: AsyncSession, instance_id: int) -> Suggestion:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError(f"Instance {instance_id} does not exist")
    links = {"instance_id": instance_id, "process_id": instance.process_id}
    with events.span("suggest_escalation", **links) as span:
        context, types = await _context(session, instance)
        shown = {k: v for k, v in context.items() if k not in {"version_id", "paid"}}
        prompt = json.dumps(shown, ensure_ascii=False, default=str)
        setup = await _current_assistant_setup(session, instance.process_id, context["version_id"])
        suggestion, trace = await llm.run(
            assistant,
            "assistant",
            prompt,
            instructions=llm.prompt("assistant"),
            setup=setup,
            instance_id=instance_id,
            deps=Deps(
                types,
                [t for t in types if t not in context["human_decision_types"]],
                set(context["evidence_refs"]),
                context["escalation"]["engine_code"] or "",
                tuple(r["name"] for r in context["escalation"]["related_cases"]),
                context["escalation"].get("first_received", ""),
                instance.name,
                context["paid"],
            ),
        )
        span.set(decision=suggestion.decision, model=trace.model)
    return suggestion


# After a resolution: the escalating rule, amended to generalise the person's decision.
class RuleSuggestion(BaseModel):
    text: str = Field(default="", max_length=RULE)  # the amended rule, in English; "" if none
    type: Literal["requirement", "prohibition"] | None = None  # None: the old rule's
    summary: Line  # one line in Spanish for the console
    rationale: Short = ""  # in Spanish: what it generalises
    evidence: list[str] = Field(min_length=1)  # references from `evidence_refs`, six kept
    # in Spanish: why no rule should learn this case, and a person must always decide it
    no_rule_reason: Short | None = None


@dataclass(frozen=True)
class RuleDeps:
    identifiers: list[str]  # this case's own names: a general rule never mentions them
    references: set[str]
    values: dict[str, set[str]] = field(default_factory=dict)  # from `known_values`
    rejected: list[str] = field(default_factory=list)  # rule texts the manager rejected ("":
    # a "no rule" answer)
    fields: frozenset[str] = frozenset()  # symbols, sources and columns a rule may read
    unusable: frozenset[str] = frozenset()  # symbols no rule reads (free text, trace only)
    names: frozenset[str] = frozenset()  # decision type names: fine in the Spanish text


# Words of a rule's expressions that are not fields.
BUILTINS = {"round", "abs", "len", "min", "max", "sum", "none", "true", "false", "and", "or"}
BUILTINS |= {"not", "in", "is", "decimal", "str", "int", "float", "cents", "others", "_instance"}
# "The data does not say": then no rule can say it either.
NO_DATA = re.compile(
    r"no hay datos?|no consta|sin datos|no (?:está|figura|aparece) en (?:los datos|ningun)"
    r"|not in the data|no data",
    re.IGNORECASE,
)


def fields_read(text: str) -> set[str]:
    """What a rule text names as data: identifiers in backticks, and snake_case or dotted
    words outside them (`orders.total_amount`, vat_rate)."""
    names = set()
    for quoted in re.findall(r"`([^`]+)`", text):
        names |= {w for w in re.findall(r"[A-Za-z_]\w*", quoted) if re.search("[a-z]", w)}
    bare = re.sub(r"`[^`]*`", " ", text)
    names |= set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", bare))
    for source, column in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", bare):
        names |= {source, column}
    return {n for n in names if n.casefold() not in BUILTINS}


reviewer_agent = Agent(
    None, output_type=RuleSuggestion, deps_type=RuleDeps, name="reviewer_agent", retries=1
)


@reviewer_agent.output_validator
def _general(ctx: RunContext[RuleDeps], suggestion: RuleSuggestion) -> RuleSuggestion:
    # trivial problems are fixed here, not sent back
    suggestion.summary = tidy(suggestion.summary, 1)
    suggestion.rationale = tidy(suggestion.rationale, 2)
    suggestion.no_rule_reason = tidy(suggestion.no_rule_reason, 2)
    suggestion.evidence = cited(suggestion.evidence, ctx.deps.references)
    if suggestion.no_rule_reason:
        suggestion.text = ""  # "no rule" wins over a rule given with it
        if "" in ctx.deps.rejected:
            raise ModelRetry(
                "the manager already rejected a no-rule answer (rejected_suggestions): "
                "propose the narrowest general rule instead"
            )
    elif not (suggestion.text.strip() and suggestion.rationale.strip()):
        raise ModelRetry("give text and rationale, or no_rule_reason")
    text = suggestion.text.casefold()
    named = [i for i in ctx.deps.identifiers if i.casefold() in text]
    if named:
        raise ModelRetry(
            f"the rule names this case ({', '.join(named)}): state conditions on symbol "
            "values, categories, thresholds or source data only"
        )
    if " ".join(text.split()) in {" ".join(r.casefold().split()) for r in ctx.deps.rejected}:
        raise ModelRetry(
            "the manager already rejected this rule (rejected_suggestions): propose a "
            "different one that answers the reject reason"
        )
    deps = ctx.deps
    if suggestion.text.strip() and deps.fields:
        read = fields_read(suggestion.text)
        known = {f.casefold() for f in deps.fields}
        unknown = sorted(f for f in read if f.casefold() not in known)
        unusable = sorted(f for f in read if f in deps.unusable)
        if unknown or unusable or not read & deps.fields:
            raise ModelRetry(
                (f"the rule reads {unknown + unusable}, which no rule can read. " if read else "")
                + "A rule compiles only on available_fields; name them in backticks: "
                + ", ".join(sorted(deps.fields - deps.unusable))
                + ". If none captures the person's reason, answer no_rule_reason"
            )
        if NO_DATA.search(f"{suggestion.summary} {suggestion.rationale}"):
            raise ModelRetry(
                "you say the data does not carry it: then no rule can read it. Answer "
                "no_rule_reason instead of a rule"
            )
    plain(
        set(deps.names),
        summary=suggestion.summary,
        rationale=suggestion.rationale,
        no_rule_reason=suggestion.no_rule_reason,
    )
    invented = made_up_values(
        f"{suggestion.text} {suggestion.rationale} {suggestion.no_rule_reason or ''}",
        ctx.deps.values,
    )
    if invented:
        raise ModelRetry(
            "these values are not in case.symbols or related_cases: "
            + ", ".join(f"{name} {value}" for name, value in invented)
            + ". The true ones: "
            + "; ".join(
                f"{name}: {', '.join(sorted(ctx.deps.values[name]))}"
                for name in sorted({name for name, _ in invented})
            )
            + ". Read every fact from the context; never infer one"
        )
    return suggestion


def known_values(*symbol_sets: dict[str, Any]) -> dict[str, set[str]]:
    """The identifier-like values the model was shown (letters and digits: a nif, an iban,
    an order), by symbol. Amounts, rates and dates are left out: a rule may state a new
    threshold. So is free text, over 40 characters."""
    known: dict[str, set[str]] = {}
    for symbols in symbol_sets:
        for name, value in symbols.items():
            value = str(value or "")
            if 6 <= len(value) <= 40 and re.search(r"[A-Za-z]", value) and re.search(r"\d", value):
                known.setdefault(name, set()).add(value.upper())
    return known


def made_up_values(text: str, known: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Tokens of `text` written like a value of a symbol (same runs of letters, digits and
    separators: B96233419 is one letter and eight digits) that are none of its values shown:
    a nif or an order the model invented, as in "the other invoice is from B12345678"."""
    found = set()
    for name, values in known.items():
        for value in values:
            shape = "".join(
                f"[A-Za-z]{{{len(run)}}}"
                if run.isalpha()
                else rf"\d{{{len(run)}}}"
                if run.isdigit()
                else re.escape(run)
                for run in re.findall(r"[A-Za-z]+|\d+|.", value)
            )
            for token in re.findall(rf"(?<![A-Za-z\d]){shape}(?![A-Za-z\d])", text):
                if token.upper() not in values:
                    found.add((name, token))
    return sorted(found)


def identifiers(instance: Instance) -> list[str]:
    """What names this one case: its file, and the invoice number and file id it carries.
    Short values are left out, since they also appear in amounts and thresholds."""
    symbols = flatten_symbols(instance.symbols or {})
    names = {
        instance.name,
        instance.name.rsplit(".", 1)[0],
        *(str(symbols.get(k) or "") for k in ("file_id", "invoice_number")),
    }
    return sorted(n for n in names if len(n.strip()) >= 4)


async def suggest_rule(
    session: AsyncSession, instance: Instance, engine: Decision, resolution: Decision, rule_id: int
) -> RuleSuggestion:
    """Amend rule `rule_id`, the one escalation rule that fired in `engine`, so that cases
    like this one get `resolution`'s decision. The caller already checked it is learnable
    (`proposals.service.learnable`) and records the `suggest_rule` span. The context is
    small on purpose: no file text, and only resolutions of cases the same rule escalated."""
    from app.features.decisions.service import current_sources
    from app.features.versions.configuration import rules as frozen_rules
    from app.features.versions.model import ProcessVersion

    version = await session.get(ProcessVersion, engine.version_id)
    rules = {r.id: r for r in frozen_rules(version.snapshot)}
    old = rules[rule_id]
    history = await session.execute(
        select(Decision, Instance.symbols)
        .join(Instance, Decision.instance_id == Instance.id)
        .where(Instance.process_id == instance.process_id, Instance.id != instance.id)
        .order_by(Decision.id)
    )
    engine_of: dict[int, Decision] = {}
    similar = []
    for d, symbols in history:  # people's resolutions of other cases this rule escalated
        if d.author == ENGINE:
            engine_of[d.instance_id] = d
            continue
        fired = engine_of[d.instance_id].results if d.instance_id in engine_of else []
        if any(r.get("rule_id") == rule_id and r.get("fires") for r in fired):
            similar.append(
                {
                    "ref": f"resolution:{d.id}",
                    "symbols": flatten_symbols(symbols or {}),
                    "decision": d.decision,
                    "reason": d.reason,
                }
            )
    similar = similar[-MAX_RESOLUTIONS:]
    sources = await current_sources(session, instance.process_id)
    case = flatten_symbols(instance.symbols or {})
    evidence = next(
        (r.get("reason") or "" for r in engine.results if r.get("rule_id") == rule_id), ""
    )
    related = await related_cases(session, instance, evidence)
    rejected = [
        {"text": p.payload.get("text"), "reject_reason": (p.outcome or {}).get("reason")}
        for p in await session.scalars(
            select(ManagerProposal)
            .where(
                ManagerProposal.instance_id == instance.id,
                ManagerProposal.kind == "rule",
                ManagerProposal.status == "rejected",
            )
            .order_by(ManagerProposal.id)
        )
    ]
    context = {
        "use_case_description": version.snapshot["process"]["description"],
        "decision_types": version.snapshot["process"]["decision_types"],
        "case": {"symbols": case},
        "escalation": {
            "reason": engine.reason,
            "rule": {"id": old.id, "text": old.text, "type": old.type, "decision": old.decision},
            "rule_evidence": evidence,  # what the rule itself reported when it fired
            "related_cases": related,
            "other_rules_fired": [
                {"id": r["rule_id"], "decision": rules[r["rule_id"]].decision}
                for r in engine.results
                if r.get("fires") and r.get("rule_id") != rule_id and r.get("rule_id") in rules
            ],
        },
        "resolution": {
            "ref": f"resolution:{resolution.id}",
            "decision": resolution.decision,
            "reason": resolution.reason,
        },
        "similar_resolutions": similar,
        "rejected_suggestions": rejected,
        "sources": {name: sorted(rows[0]) if rows else [] for name, rows in sources.items()},
    }
    # ponytail: a symbol whose description says no rule reads it (free text, trace only) is
    # left out; a `readable` flag on symbols if another pack words it differently.
    symbols = version.snapshot["process"]["symbols"]
    unusable = {s["name"] for s in symbols if "no rule" in s.get("description", "").casefold()}
    context["available_fields"] = {
        "symbols": {
            s["name"]: s.get("description", "") for s in symbols if s["name"] not in unusable
        },
        "sources": context["sources"],
    }
    fields = (
        {s["name"] for s in symbols}
        | set(sources)
        | {c for columns in context["sources"].values() for c in columns}
        | fields_read(old.text)
    )
    context["evidence_refs"] = sorted(
        {f"symbol:{name}" for name in instance.symbols or {}}
        | {f"rule:{rule_id}", context["resolution"]["ref"]}
        | {r["ref"] for r in similar}
    )
    suggestion, _ = await llm.run(
        reviewer_agent,
        "assistant",
        json.dumps(context, ensure_ascii=False, default=str),
        instructions=llm.prompt("reviewer_agent", "shared"),
        setup=await _current_assistant_setup(session, instance.process_id, version.id),
        deps=RuleDeps(
            identifiers(instance) + [r["name"] for r in related],
            set(context["evidence_refs"]),
            known_values(case, *(r["symbols"] for r in related + similar)),
            [r["text"] or "" for r in rejected],
            frozenset(fields),
            frozenset(unusable),
            frozenset(t["name"] for t in version.snapshot["process"]["decision_types"]),
        ),
        instance_id=instance.id,
    )
    return suggestion
