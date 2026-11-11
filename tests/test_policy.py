import pytest

from superagent.core.model import Mandate, Trigger
from superagent.core.policy import Admission, Policy, Rule, SourcePolicy


def trig(actor="alice", addressed="mention", bot=False, observed=False, source="github"):
    return Trigger(id="t", source=source, kind="comment", actor=actor, text="hi", threads=("github:o/r#1",),
                   actor_is_bot=bot, addressed=addressed, observed=observed)


def policy(mode="observe", admission=Admission(), rules=()):
    return Policy(mode=mode, rules=rules,
                  sources={"github": SourcePolicy(allowed=("Alice",), bots=("dependabot[bot]",), admission=admission)})


def test_allowed_person_addressing_the_agent_is_explicit():
    result = policy().gate(trig())
    assert result.admitted and result.strength == "explicit" and result.basis == "allowlist"


def test_allowlist_is_case_insensitive():
    assert policy().gate(trig(actor="ALICE")).strength == "explicit"


def test_unlisted_person_addressing_the_agent_is_ignored_by_default():
    result = policy().gate(trig(actor="mallory"))
    assert not result.admitted
    assert "unlisted addressed" in result.reason


def test_unaddressed_message_without_receive_all_is_not_admitted():
    assert not policy().gate(trig(addressed="")).admitted


def test_listed_unaddressed_observed_message_is_inferred():
    result = policy().gate(trig(addressed="", observed=True))
    assert result.admitted and result.strength == "inferred" and result.cell == "listed_unaddressed"


def test_inferred_candidates_are_refused_when_mode_is_off():
    result = policy(mode="off").gate(trig(addressed="", observed=True))
    assert not result.admitted and "off" in result.reason


def test_explicit_requests_work_in_off_mode():
    assert policy(mode="off").gate(trig()).strength == "explicit"


def test_unlisted_addressed_cell_set_to_explicit_admits_anyone():
    result = policy(admission=Admission(unlisted_addressed="explicit")).gate(trig(actor="bob"))
    assert result.strength == "explicit" and result.basis == "admission"


def test_unaddressed_cells_cannot_grant_explicit_strength():
    result = policy(admission=Admission(unlisted_unaddressed="explicit")).gate(
        trig(actor="bob", addressed="", observed=True))
    assert result.strength == "inferred"


def test_listed_bot_is_inferred_only_when_addressed_or_observed():
    assert policy().gate(trig(actor="dependabot[bot]", bot=True)).strength == "inferred"
    assert not policy().gate(trig(actor="dependabot[bot]", bot=True, addressed="")).admitted


def test_unlisted_bot_is_refused():
    assert not policy().gate(trig(actor="other[bot]", bot=True)).admitted


def test_event_without_sender_is_refused():
    assert not policy().gate(trig(actor="")).admitted


def test_followup_from_listed_person_is_explicit_and_from_others_inferred():
    assert policy().gate(trig(addressed=""), followup=True).strength == "explicit"
    assert policy().gate(trig(actor="bob", addressed=""), followup=True).strength == "inferred"


def test_unknown_source_is_refused():
    assert not policy().gate(trig(source="slack")).admitted


def test_version_changes_with_policy_content():
    assert policy().version == policy().version
    assert policy().version != policy(mode="auto").version
    assert policy().version != policy(rules=(Rule("r1", "Rule", "Do it"),)).version


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        Policy(mode="sometimes")  # type: ignore[arg-type]


def test_admits_inferred_needs_auto_mode_and_enabled_rules():
    rules = (Rule("r1", "One", "x"), Rule("r2", "Two", "y", enabled=False))
    assert not policy(mode="observe", rules=rules).admits_inferred(["r1"])
    assert policy(mode="auto", rules=rules).admits_inferred(["r1"])
    assert not policy(mode="auto", rules=rules).admits_inferred(["r2"])
    assert not policy(mode="auto", rules=rules).admits_inferred([])


def mandate(**kw):
    base = dict(policy_version="p", strength="explicit", basis="allowlist", scope="s", requester="alice",
                source="github")
    base.update(kw)
    return Mandate(**base)


def test_revalidate_allowlist_mandate_follows_the_list():
    assert policy().revalidate(mandate())
    assert not policy().revalidate(mandate(requester="bob"))


def test_revalidate_fails_when_source_no_longer_listens():
    assert not policy().revalidate(mandate(), source_listens=False)


def test_revalidate_inferred_rule_mandate_needs_auto_and_enabled_rule():
    rules = (Rule("r1", "One", "x"),)
    m = mandate(strength="inferred", basis="admission", cell="listed_unaddressed", rules=("r1",))
    assert policy(mode="auto", rules=rules).revalidate(m)
    assert not policy(mode="observe", rules=rules).revalidate(m)


def test_revalidate_admission_mandate_needs_original_strength():
    m = mandate(requester="bob", basis="admission", cell="unlisted_addressed")
    assert policy(admission=Admission(unlisted_addressed="explicit")).revalidate(m)
    assert not policy(admission=Admission(unlisted_addressed="inferred")).revalidate(m)


def test_revalidate_bot_mandate_needs_bot_on_list():
    m = mandate(strength="inferred", basis="bot", requester="dependabot[bot]", rules=("r1",))
    rules = (Rule("r1", "One", "x"),)
    assert policy(mode="auto", rules=rules).revalidate(m)
    assert not policy(mode="auto", rules=rules).revalidate(mandate(strength="inferred", basis="bot",
                                                                   requester="x[bot]", rules=("r1",)))
