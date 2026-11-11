import json

from superagent.agents.assessor import OUTPUT_SCHEMA, Assessor, render_candidate, to_outcome
from superagent.agents.prompts import PromptLibrary
from superagent.llm.client import ModelResponse, TransientModelError
from superagent.llm.fake import ScriptedModelClient, text
from tests.conftest import make_trigger


def answer(**fields):
    base = {"decision": "accept", "reason": "clear request", "title": "Fix typo", "scope": "Fix the README typo",
            "name": "fix-readme-typo", "acknowledgement": "On it.", "note": "", "ticket_id": "",
            "rules": [{"id": "typos", "matches": True, "why": "typo"}, {"id": "deps", "matches": False, "why": "-"}]}
    base.update(fields)
    return text(json.dumps(base))


def candidate(engine):
    engine.intake(make_trigger(text="@agent fix the typo in README", title="README typo"))
    [cand] = engine.start_evals()
    return cand


def test_candidate_card_shows_messages_and_tickets(engine):
    engine.intake(make_trigger(text="earlier remark", actor="bob", addressed=""))
    card = render_candidate(candidate(engine))
    assert 'strength="explicit"' in card
    assert "earlier remark" not in card.split("New messages to judge:")[1]
    assert "fix the typo in README" in card
    assert "Tickets on this conversation:\n- none" in card


def test_assess_sends_schema_and_policy_and_parses_the_outcome(engine):
    client = ScriptedModelClient([answer()])
    outcome = Assessor(client, PromptLibrary(), "claude-opus-5-5").assess(candidate(engine), engine.policy)
    assert outcome.kind == "accept" and outcome.rules == ("typos",) and outcome.name == "fix-readme-typo"
    request = client.requests[0]
    assert request["output_schema"] == OUTPUT_SCHEMA
    assert request["effort"] == "low"
    assert engine.policy.version in request["system"]


def test_model_failures_become_unavailable(engine):
    cand = candidate(engine)
    client = ScriptedModelClient([TransientModelError("overloaded"), text("not json"),
                                  ModelResponse(content=[], stop_reason="refusal", refusal_category="cyber")])
    assessor = Assessor(client, PromptLibrary(), "m")
    for expected in ("overloaded", "not valid JSON", "declined"):
        outcome = assessor.assess(cand, engine.policy)
        assert outcome.kind == "unavailable" and expected in outcome.reason


def test_to_outcome_normalizes_fields():
    outcome = to_outcome({"decision": "explode", "note": " ".join(["w"] * 50), "rules": [{"id": "x", "matches": True}]})
    assert outcome.kind == "ignore"
    assert len(outcome.note.split()) == 30
    assert outcome.rules == ("x",)


def test_schema_is_strict():
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
