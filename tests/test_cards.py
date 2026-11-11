from superagent.cards import assignment_card, input_card
from superagent.core.model import Assignment, Input, Mandate, Ticket
from superagent.core.policy import Rule
from tests.conftest import make_policy, make_trigger


def ticket(**kw):
    t = Ticket(id="tk_1", name="fix-typo", title="Fix typo", decision_id="d", origin_thread="github:o/r#1",
               origin_trigger="t", mandate=Mandate("p", "inferred", "admission", "Fix the typo", "alice", "github",
                                                   rules=("typos",)), assignment_ids=["as_1"])
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def test_assignment_card_names_scope_requester_and_rules():
    policy = make_policy(rules=[Rule("typos", "Fix typos", "x")])
    card_id, text = assignment_card(ticket(acknowledged=True), Assignment("as_1", "tk_1"), policy)
    assert card_id == "as_1:card"
    assert "Scope: Fix the typo" in text and "alice on github (inferred request)" in text
    assert "typos (Fix typos)" in text and "already acknowledged" in text
    assert "new assignment" not in text


def test_assignment_card_warns_on_a_later_assignment():
    _, text = assignment_card(ticket(assignment_ids=["as_1", "as_2"]), Assignment("as_2", "tk_1"), make_policy())
    assert "new assignment for an existing ticket" in text


def test_message_card_carries_ids_ownership_and_escaped_attributes():
    trigger = make_trigger(text='Please fix "this" <now>', actor='al"ice', tid="github:comment:9")
    text = input_card(Input(id="in_1", kind="trigger", trigger_id=trigger.id, owned=True), trigger)
    assert 'message_id="github:comment:9"' in text and 'owned="true"' in text
    assert 'from="al&quot;ice"' in text and 'addressed="mention"' in text
    assert text.endswith('Please fix "this" <now>\n</input>')


def test_note_card_and_clipping():
    text = input_card(Input(id="in_2", kind="recovery", text="x" * 30000), None)
    assert text.startswith('<input id="in_2" kind="recovery">') and "more characters" in text
