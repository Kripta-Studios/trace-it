"""Against the local database (`make test-db`); skipped if absent. The model is scripted:
no network."""

import json
import socket
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.database import engine, session_factory
from app.core.events import Event
from app.features.agents import assistant, llm
from app.features.decisions.model import Decision
from app.features.ingestion.model import File, Instance
from app.features.processes.model import DecisionType, Process
from app.features.rules.model import Rule
from app.main import app
from tests.support import rows
from tests.support.models import per_role, retry_prompts, user_json
from tests.support.users import manager_client


def _db_available() -> bool:
    url = make_url(settings.database_url)
    try:
        socket.create_connection((url.host, url.port or 5432), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Local Postgres not available")

SUGGESTION = {
    "decision": "NO_PAGAR",
    "reasoning": "amount=5000 (text) exceeds the limit of the rule 'amount > 1000'",
    "why": ["The invoice is over 1000, and amounts above it need a person."],
    "options": [
        {
            "decision": "PAGAR",
            "consequence": "Se paga si se añade la regla: hasta 4000 no escala.",
            "rule": "Amend rule 1: amount > 1000, unless amount <= 4000.",
        },
        {
            "decision": "NO_PAGAR",
            "consequence": "No se paga por la regla nueva: ACME no factura por encima de 4000.",
            "rule": "If amount (invoice text) > 4000 and supplier (Excel) = 'ACME', do not pay",
        },
    ],
    "evidence": ["symbol:amount", "file"],
    "proposed_rule": "If amount (invoice text) > 4000 and supplier (Excel) = 'ACME', do not pay",
    "proposed_type": "prohibition",
}
# "No rule" is an answer too: a person must always look at cases like this one.
NO_RULE = {
    **SUGGESTION,
    "options": [{**o, "rule": None} for o in SUGGESTION["options"]],
    "proposed_rule": None,
    "proposed_type": None,
    "no_rule_reason": "Falta un dato que ninguna regla puede suplir: una persona debe pedirlo.",
}


DESCRIPTION = "Amounts in whole cents; if a value is missing, the rule does not fire."


@pytest.fixture
async def case(request):
    """A process with an escalated instance, a past human resolution and a pending one. The
    human-queue type is named ESCALAR unless the test passes another name as param."""
    escalate = getattr(request, "param", "ESCALAR")
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as s:
        process = await rows.process(s, f"assistant-{suffix}", DESCRIPTION)
        s.add_all(
            [
                DecisionType(process_id=process.id, name=escalate, priority=3, requires_human=True),
                DecisionType(process_id=process.id, name="NO_PAGAR", priority=2),
                DecisionType(process_id=process.id, name="PAGAR", priority=1, is_default=True),
            ]
        )
        await s.flush()
        rule = Rule(
            process_id=process.id,
            text="If amount > 1000, escalate",
            type="prohibition",
            decision=escalate,
            status="active",
        )
        file = File(hash=uuid.uuid4().hex, name="f.pdf", content=b"x", text="Total: 5000 EUR")
        s.add_all([rule, file])
        await s.flush()
        symbols = {"amount": {"value": 5000, "origin": "text"}}
        escalated, old, pending = (
            Instance(process_id=process.id, file_hash=file.hash, name=n, symbols=symbols)
            for n in ("a", "b", "c")
        )
        s.add_all([escalated, old, pending])
        await s.flush()
        results = [{"rule_id": rule.id, "hash": "h", "fires": True, "reason": "amount > 1000"}]
        s.add_all(
            [
                Decision(
                    instance_id=escalated.id,
                    decision=escalate,
                    results=results,
                    rules_hash="h",
                    author="engine",
                ),
                Decision(
                    instance_id=old.id,
                    decision="NO_PAGAR",
                    results=results,
                    rules_hash="h",
                    author="Ana",
                    reason="ACME does not charge above 4000",
                ),
            ]
        )
        await s.commit()
        yield {"escalated": escalated.id, "pending": pending.id, "old": old.id}
    await engine.dispose()  # connections are bound to this test's event loop


def script(monkeypatch, replies: list[dict]) -> list:
    seen: dict = {}
    monkeypatch.setattr(llm, "model_for", per_role({"assistant": replies}, seen))
    return seen.setdefault("assistant", [])


async def test_suggests_and_records_an_event(case, monkeypatch) -> None:
    calls = script(monkeypatch, [SUGGESTION])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert suggestion == assistant.Suggestion(**SUGGESTION)

    context = user_json(calls[0])
    assert context["use_case_description"] == DESCRIPTION
    assert context["decision_types"] == ["ESCALAR", "NO_PAGAR", "PAGAR"]
    assert context["human_decision_types"] == ["ESCALAR"]
    assert context["escalation"]["fired_rules"][0]["text"].startswith("If amount")
    assert context["human_resolutions"][0]["reason"] == "ACME does not charge above 4000"
    assert context["case"]["file_text"] == "Total: 5000 EUR"

    async with session_factory() as s:
        event = await s.scalar(
            select(Event).where(
                Event.instance_id == case["escalated"], Event.step == "suggest_escalation"
            )
        )
    assert event.step == "suggest_escalation"
    assert event.data["model"] == "fake/model"
    assert event.data["decision"] == "NO_PAGAR"
    assert event.duration_ms is not None


@pytest.mark.parametrize("endpoint", ["suggestion", "proposal", "rule-proposal"])
async def test_new_advice_uses_current_model_with_unchanged_historical_context(
    case, monkeypatch, endpoint
):
    from app.features.versions import configuration, service
    from app.features.versions.model import ProcessVersion

    async with session_factory() as session:
        instance = await session.get(Instance, case["escalated"])
        process = await session.get(Process, instance.process_id)
        original = await configuration.workspace(session, process.id)
        original["agents"]["assistant"]["settings"].update(
            model="helmcode:deepseek-v4-flash",
            fallback_models=["helmcode:glm5.3-flash", "helmcode:qwen3.6"],
        )
        historical = await service.publish_snapshot(
            session, process, original, "fixture", "Old provider", {}
        )
        engine_decision = await session.scalar(
            select(Decision).where(Decision.instance_id == instance.id)
        )
        engine_decision.version_id = historical.id
        engine_decision.reason = "amount > 1000"
        current = deepcopy(original)
        current["agents"]["assistant"]["settings"].update(
            model="openai-responses:gpt-6-luna", fallback_models=[]
        )
        current["process"]["description"] = "New policy, not this historical case's policy"
        current["rules"][0]["text"] = "New rule text, not the historical escalation"
        published = await service.publish_snapshot(
            session, process, current, "fixture", "Retire old provider", {}
        )
        if endpoint == "rule-proposal":
            session.add(
                Decision(
                    instance_id=instance.id,
                    version_id=historical.id,
                    decision="PAGAR",
                    author="Manager",
                    reason="Reviewed and approved",
                    results=[],
                    rules_hash="h",
                )
            )
        await session.commit()
        historical_id, current_id = historical.id, published.id
        historical_hash = historical.content_hash
        before_decisions = [
            (d.id, d.version_id, d.decision, deepcopy(d.results), d.reason)
            for d in await session.scalars(
                select(Decision).where(Decision.instance_id == instance.id).order_by(Decision.id)
            )
        ]

    calls = []

    async def answer(agent, role, prompt, **kwargs):
        calls.append((json.loads(prompt), kwargs["setup"]))
        response = (
            assistant.RuleSuggestion(
                summary="No additional rule",
                rationale="The existing evidence remains unchanged",
                evidence=["symbol:amount"],
                no_rule_reason="The manager has already reviewed this case",
            )
            if endpoint == "rule-proposal"
            else assistant.Suggestion(**SUGGESTION)
        )
        return response, SimpleNamespace(model=kwargs["setup"].settings.model)

    monkeypatch.setattr(llm, "run", answer)
    async with manager_client() as api:
        response = await api.request(
            "GET" if endpoint == "suggestion" else "POST",
            f"/instances/{case['escalated']}/{endpoint}",
        )
    assert response.status_code == (200 if endpoint == "suggestion" else 201), response.text
    [(context, setup)] = calls
    assert setup.settings.model == "openai-responses:gpt-6-luna"
    assert setup.settings.fallback_models == []
    assert context["use_case_description"] == DESCRIPTION
    historical_rule = (
        context["escalation"]["rule"]
        if endpoint == "rule-proposal"
        else context["escalation"]["fired_rules"][0]
    )
    assert historical_rule["text"] == "If amount > 1000, escalate"
    async with session_factory() as session:
        saved = await session.get(ProcessVersion, historical_id)
        assert saved.snapshot == original and saved.content_hash == historical_hash
        assert (await session.get(ProcessVersion, current_id)).snapshot == current
        after_decisions = [
            (d.id, d.version_id, d.decision, d.results, d.reason)
            for d in await session.scalars(
                select(Decision)
                .where(Decision.instance_id == case["escalated"])
                .order_by(Decision.id)
            )
        ]
        assert after_decisions == before_decisions
        span = await session.scalar(
            select(Event).where(
                Event.instance_id == case["escalated"],
                Event.step
                == ("suggest_rule" if endpoint == "rule-proposal" else "suggest_escalation"),
            )
        )
        assert span.data["context_version_id"] == historical_id
        assert span.data["model_version_id"] == current_id


@pytest.mark.parametrize("case", ["MANUAL_CHECK"], indirect=True)
async def test_human_type_with_another_name(case, monkeypatch) -> None:
    calls = script(monkeypatch, [SUGGESTION])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert suggestion.decision == "NO_PAGAR"
    context = user_json(calls[0])
    assert context["human_decision_types"] == ["MANUAL_CHECK"]
    assert context["escalation"]["current_decision"] == "MANUAL_CHECK"


async def test_invalid_decision_retries_once(case, monkeypatch) -> None:
    calls = script(monkeypatch, [{**SUGGESTION, "decision": "REJECT"}, SUGGESTION])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert suggestion.decision == "NO_PAGAR"
    assert len(calls) == 2
    async with session_factory() as s:
        event = await s.scalar(
            select(Event).where(
                Event.instance_id == case["escalated"], Event.step == "suggest_escalation"
            )
        )
        run = await s.scalar(
            select(Event).where(Event.trace_id == event.trace_id, Event.step == "llm_run")
        )
    assert run.parent_id == event.span_id
    assert run.data["retries"] == 1 and run.data["agent"] == "assistant"
    [retry] = run.data["retry_prompts"]
    assert "REJECT" in retry and run.data["output"]["decision"] == "NO_PAGAR"


async def test_four_invalid_decisions_give_502(case, monkeypatch) -> None:
    script(monkeypatch, [{**SUGGESTION, "decision": "REJECT"}] * 4)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        r = await api.get(f"/instances/{case['escalated']}/suggestion")
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "llm_error"


async def test_no_llm_key_gives_502(case, monkeypatch) -> None:
    for key in ("HELMCODE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        r = await api.get(f"/instances/{case['escalated']}/suggestion")
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "llm_error"


async def test_not_escalated_gives_409(case, monkeypatch) -> None:
    script(monkeypatch, [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        r = await api.get(f"/instances/{case['pending']}/suggestion")
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "conflict"
        r = await api.get("/instances/0/suggestion")
        assert r.status_code == 404, r.text


async def test_a_long_answer_is_trimmed_without_asking_again(case, monkeypatch) -> None:
    """Latency: over the character caps or the sentence limits, the text is cut in place;
    backticks and rule codes are rewritten; a made-up reference is dropped. One call."""
    essay = {
        **SUGGESTION,
        "why": ["Se escaló por el `importe` y R01.\nAdemás hay otras cosas que contar aquí."],
        "reasoning": "x" * 400,
        "evidence": ["symbol:amount", "escalation: amount > 1000"],
    }
    calls = script(monkeypatch, [essay])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert len(calls) == 1 and retry_prompts(calls[-1]) == []
    assert suggestion.why == ["Se escaló por el importe y la regla 1."]
    assert len(suggestion.reasoning) == 320 and suggestion.reasoning.endswith("…")
    assert suggestion.evidence == ["symbol:amount"]


async def test_an_extra_option_and_a_file_name_need_no_second_call(case, monkeypatch) -> None:
    """Keeping it escalated is not an option when a rule escalated it: dropped in place. A
    file name is fine for a manager, even with an underscore."""
    extra = {
        **SUGGESTION,
        "reasoning": "Como hosteleria_A_F26-0726.pdf, se paga.",
        "options": [*SUGGESTION["options"], KEEP],
    }
    calls = script(monkeypatch, [extra])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert len(calls) == 1 and retry_prompts(calls[-1]) == []
    assert [o.decision for o in suggestion.options] == ["PAGAR", "NO_PAGAR"]
    assert suggestion.reasoning == extra["reasoning"]


async def test_every_option_carries_its_rule(case, monkeypatch) -> None:
    bare = {**SUGGESTION, "options": [{**SUGGESTION["options"][0], "rule": None}]}
    bare["options"].append(SUGGESTION["options"][1])
    calls = script(monkeypatch, [bare, SUGGESTION])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert all(o.rule for o in suggestion.options)
    [retry] = retry_prompts(calls[-1])
    assert "every option needs the rule that justifies it: ['PAGAR']" in retry


async def test_no_rule_is_a_valid_answer(case, monkeypatch) -> None:
    # no rule and a rule at once: "no rule" wins, fixed in place
    half = {**NO_RULE, "proposed_rule": "Pay it.", "proposed_type": "requirement"}
    calls = script(monkeypatch, [half])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert suggestion.proposed_rule is None and suggestion.proposed_type is None
    assert suggestion.no_rule_reason.startswith("Falta") and len(calls) == 1
    assert suggestion.decision == "NO_PAGAR"  # still a final decision
    # neither a rule nor a reason: sent back
    calls = script(monkeypatch, [{**NO_RULE, "no_rule_reason": None}, NO_RULE])
    async with session_factory() as s:
        await assistant.suggest(s, case["escalated"])
    [retry] = retry_prompts(calls[-1])
    assert "or no_rule_reason" in retry


async def escalate_as(case, reason: str, fired_reason: str | None = None) -> None:
    """The escalated case's engine decision, now with `reason` (and its fired rule's)."""
    async with session_factory() as s:
        decision = await s.scalar(select(Decision).where(Decision.instance_id == case["escalated"]))
        decision.reason = reason
        if fired_reason:
            decision.results = [{**decision.results[0], "reason": fired_reason}]
        await s.commit()


KEEP = {"decision": "ESCALAR", "consequence": "Mantener escalado y pedir el importe al proveedor."}


async def test_an_engine_escalation_keeps_a_person_deciding(case, monkeypatch) -> None:
    """MISSING_DATA and the other engine codes: no rule decides them. The advice must offer
    to keep the case escalated and say a person decides."""
    await escalate_as(case, "MISSING_DATA: amount")
    kept = {**NO_RULE, "decision": "ESCALAR", "options": [*NO_RULE["options"], KEEP]}
    calls = script(monkeypatch, [NO_RULE, kept])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert suggestion.decision == "ESCALAR" and suggestion.no_rule_reason
    assert user_json(calls[0])["escalation"]["engine_code"] == "MISSING_DATA"
    [first] = retry_prompts(calls[-1])
    assert "for: NO_PAGAR, PAGAR, ESCALAR (ESCALAR: 'mantener escalado / pedir el dato'" in first
    calls = script(monkeypatch, [{**kept, "no_rule_reason": None}, kept])
    async with session_factory() as s:
        await assistant.suggest(s, case["escalated"])
    [second] = retry_prompts(calls[-1])
    assert "MISSING_DATA is an engine escalation, not a rule" in second


async def test_jargon_and_closing_without_a_person_are_sent_back(case, monkeypatch) -> None:
    closes = {**SUGGESTION, "reasoning": "Se paga y el caso se cierra sin intervención humana."}
    coded = {**SUGGESTION, "why": ["El `amount` de symbol:amount supera la regla R01."]}
    for wrong, said in (
        (closes, "reasoning says the case closes without a person"),
        (coded, "why[0] uses ['symbol:']"),  # the backticks and R01 were fixed in place
    ):
        calls = script(monkeypatch, [wrong, SUGGESTION])
        async with session_factory() as s:
            suggestion = await assistant.suggest(s, case["escalated"])
        assert suggestion == assistant.Suggestion(**SUGGESTION)
        [retry] = retry_prompts(calls[-1])
        assert said in retry


async def test_both_invoices_of_one_order_are_advised_consistently(case, monkeypatch) -> None:
    """A fired rule that names another case (a duplicate order): the advice says which one
    is paid, or that a person must compare them. NO_PAGAR on both means never paid."""
    async with session_factory() as s:
        (await s.get(Instance, case["old"])).name = "factura_41082.pdf"
        await s.commit()
    await escalate_as(case, "x", "Same purchase order as: factura_41082.pdf")
    paired = {
        **SUGGESTION,
        "decision": "PAGAR",
        "reasoning": "Se paga la primera recibida, esta; factura_41082.pdf es el duplicado.",
    }
    calls = script(monkeypatch, [SUGGESTION, paired])
    async with session_factory() as s:
        suggestion = await assistant.suggest(s, case["escalated"])
    assert suggestion.reasoning == paired["reasoning"]
    [related] = user_json(calls[0])["escalation"]["related_cases"]
    assert (related["name"], related["decision"], related["received"]) == (
        "factura_41082.pdf",
        "NO_PAGAR",
        "after",
    )
    assert user_json(calls[0])["escalation"]["first_received"] == "a"  # this case, by name
    [retry] = retry_prompts(calls[-1])
    assert "involves other cases (factura_41082.pdf)" in retry


async def test_the_pair_is_advised_against_the_computed_first(case, monkeypatch) -> None:
    """Live demo bug: case 1 proposed PAGAR while saying the other invoice, "the first
    received", is paid. Which one came first is computed (invoice date, then arrival) and
    the text and the decision must agree with it."""
    async with session_factory() as s:
        (await s.get(Instance, case["old"])).name = "factura_41082.pdf"
        (await s.get(Instance, case["escalated"])).name = "catering.pdf"
        await s.commit()
    await escalate_as(case, "x", "Same purchase order as: factura_41082.pdf")
    wrong_first = {
        **SUGGESTION,
        "decision": "NO_PAGAR",
        "reasoning": "Se paga la primera recibida, factura_41082.pdf, y esta no.",
    }
    pays_other = {
        **SUGGESTION,
        "decision": "PAGAR",
        "reasoning": "Es un duplicado: se paga factura_41082.pdf y esta no.",
    }
    right = {
        **SUGGESTION,
        "decision": "PAGAR",
        "reasoning": "Esta es la primera recibida: se paga esta y factura_41082.pdf no.",
    }
    for wrong, said in (
        (wrong_first, "the first received is catering.pdf (escalation.first_received)"),
        (pays_other, "you say factura_41082.pdf is the one paid, so this case is not"),
    ):
        calls = script(monkeypatch, [wrong, right])
        async with session_factory() as s:
            suggestion = await assistant.suggest(s, case["escalated"])
        assert suggestion.decision == "PAGAR"
        [retry] = retry_prompts(calls[-1])
        assert said in retry
    calls = script(monkeypatch, [right])
    async with session_factory() as s:
        await assistant.suggest(s, case["escalated"])
    assert len(calls) == 1


def test_arrival_goes_by_invoice_date_then_by_order():
    first, later, undated = (
        Instance(id=i, name=str(i), symbols=symbols)
        for i, symbols in (
            (3, {"date": {"value": "2026-01-02", "origin": "text"}}),
            (1, {"date": {"value": "2026-02-01", "origin": "text"}}),
            (2, {}),
        )
    )
    assert sorted([undated, later, first], key=assistant.arrival) == [first, later, undated]


async def test_the_decision_assistant_runs_without_reasoning_under_a_tight_cap(monkeypatch):
    """Latency: the decision assistant shares the `assistant` role's settings with the
    reviewer agent (reasoning off, max_tokens capped); the model receives both."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from tests.support.pack import use_case

    received = []

    def answer(messages, info):
        received.append(info.model_settings)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, SUGGESTION)])

    monkeypatch.setattr(llm, "resolve", lambda name, *_: FunctionModel(answer, model_name=name))
    types = ["ESCALAR", "NO_PAGAR", "PAGAR"]
    deps = assistant.Deps(types, ["NO_PAGAR", "PAGAR"], {"symbol:amount", "file"})
    setup = llm.Setup(use_case().agents["assistant"])
    await llm.run(assistant.assistant, "assistant", "{}", instructions="", setup=setup, deps=deps)
    [model_settings] = received
    assert model_settings["openai_reasoning_effort"] == "none"
    assert model_settings["max_tokens"] <= 1500
    # a stalled request moves on fast: short timeout, no hidden SDK repeats, one retry
    assert model_settings["timeout"] <= 15
    assert setup.settings.limits["http_retries"] == 0 and setup.settings.retries == 1
