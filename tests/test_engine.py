from superagent.core.engine import Outcome
from superagent.core.model import Assignment, Decision, Report, Thread, Ticket
from superagent.core.policy import Rule
from tests.conftest import make_policy, make_trigger

ACCEPT = Outcome("accept", "clear request", title="Fix the typo", scope="Fix the typo in README",
                 name="fix-typo", acknowledgement="On it: fixing the README typo.")


def accept_one(engine, trigger=None, outcome=ACCEPT):
    trigger = trigger or make_trigger()
    assert engine.intake(trigger) == "queued"
    [candidate] = engine.start_evals()
    decision = engine.decide(candidate.eval.id, outcome)
    return decision, engine.store.get(Ticket, decision.ticket_id)


def messages(engine):
    return engine.outbox.recent()


# -- intake and eval -------------------------------------------------------------------------------------------


def test_explicit_request_becomes_ticket_with_mandate_subscription_and_acknowledgement(engine):
    trigger = make_trigger()
    decision, ticket = accept_one(engine, trigger)
    assert decision.kind == "accept" and ticket is not None
    assert ticket.name == "fix-typo"
    assert ticket.mandate.strength == "explicit" and ticket.mandate.basis == "allowlist"
    assert ticket.mandate.requester == "alice"
    assert [i.trigger_id for i in ticket.inputs if i.owned] == [trigger.id]
    thread = engine.store.get(Thread, "github:o/r#1")
    assert thread.subscription(ticket.id) is not None and not thread.queued and not thread.eval_id
    assert engine.store.get(Assignment, ticket.assignment_id).state == "pending"
    assert messages(engine)[0]["kind"] == "acknowledgement" and ticket.acknowledged
    assert engine.disposition(trigger.id)["kind"] == "decided"


def test_duplicate_trigger_is_ignored(engine):
    trigger = make_trigger(tid="github:comment:dup")
    assert engine.intake(trigger) == "queued"
    assert engine.intake(trigger) == "duplicate"


def test_message_from_unlisted_person_is_filtered(engine):
    trigger = make_trigger(actor="mallory")
    assert engine.intake(trigger) == "filtered"
    assert "admission" in engine.disposition(trigger.id)["detail"]


def test_triggers_arriving_during_an_eval_wait_for_the_next_batch(engine):
    engine.intake(make_trigger(text="@agent first"))
    [candidate] = engine.start_evals()
    engine.intake(make_trigger(text="@agent second"))
    assert engine.start_evals() == []
    engine.decide(candidate.eval.id, Outcome("ignore", "chat"))
    [second] = engine.start_evals()
    assert [t.text for t in second.triggers] == ["@agent second"]
    assert [t.text for t in second.context] == ["@agent first"]


def test_candidate_lists_every_ticket_on_the_thread(engine):
    _, ticket = accept_one(engine)
    engine.stop(ticket.id)
    engine.intake(make_trigger(text="@agent another thing"))
    [candidate] = engine.start_evals()
    assert [t.id for t in candidate.tickets] == [ticket.id]


# -- inferred candidates ---------------------------------------------------------------------------------------


def test_inferred_accept_in_observe_mode_is_recorded_as_would_accept(engine):
    trigger = make_trigger(text="typo in docs", addressed="", observed=True)
    assert engine.intake(trigger) == "queued"
    [candidate] = engine.start_evals()
    assert candidate.eval.strength == "inferred"
    decision = engine.decide(candidate.eval.id, Outcome("accept", "typo", rules=("typos",)))
    assert decision.kind == "ignore" and decision.would_accept and decision.rules == ["typos"]
    assert engine.store.count(Ticket) == 0


def test_inferred_accept_in_auto_mode_needs_a_matched_rule(engine):
    engine.set_policy(make_policy(mode="auto", rules=[Rule("typos", "Fix typos", "Fix obvious typos.")]))
    engine.intake(make_trigger(text="typo", addressed="", observed=True))
    [candidate] = engine.start_evals()
    assert engine.decide(candidate.eval.id, Outcome("accept", "no rule")).kind == "ignore"
    engine.intake(make_trigger(text="another typo", addressed="", observed=True))
    [candidate] = engine.start_evals()
    decision = engine.decide(candidate.eval.id, Outcome("accept", "typo", rules=("typos",)))
    ticket = engine.store.get(Ticket, decision.ticket_id)
    assert decision.kind == "accept" and ticket.mandate.rules == ("typos",)


def test_assessment_cannot_invent_rules(engine):
    engine.set_policy(make_policy(mode="auto", rules=[Rule("typos", "Fix typos", "x")]))
    engine.intake(make_trigger(text="refactor everything", addressed="", observed=True))
    [candidate] = engine.start_evals()
    assert engine.decide(candidate.eval.id, Outcome("accept", "x", rules=("made-up",))).kind == "ignore"


# -- follow-ups, fan-out and resume ----------------------------------------------------------------------------


def test_person_follow_up_reaches_the_ticket_without_eval(engine):
    _, ticket = accept_one(engine)
    follow_up = make_trigger(text="also fix the second typo", addressed="")
    assert engine.intake(follow_up) == "delivered"
    ticket = engine.store.get(Ticket, ticket.id)
    assert any(i.trigger_id == follow_up.id and i.owned for i in ticket.inputs)
    assert ticket.human_since_report


def test_follow_up_answers_an_open_question(engine):
    _, ticket = accept_one(engine)
    engine.assignment_started(ticket.id, ticket.assignment_id)
    engine.report(ticket.id, ticket.assignment_id, Report("waiting", "need input", question="Which file?"))
    assert engine.store.get(Ticket, ticket.id).attention == "Which file?"
    assert messages(engine)[0]["kind"] == "question"
    engine.intake(make_trigger(text="README.md", addressed=""))
    assert engine.store.get(Ticket, ticket.id).attention == ""


def test_bot_message_on_subscribed_thread_is_context_and_is_gated_separately(engine):
    _, ticket = accept_one(engine)
    bot = make_trigger(text="CI failed", actor="ci[bot]", bot=True, addressed="")
    assert engine.intake(bot) == "filtered"
    ticket = engine.store.get(Ticket, ticket.id)
    assert any(i.trigger_id == bot.id and not i.owned for i in ticket.inputs)


def test_person_message_resumes_an_ended_ticket(engine):
    _, ticket = accept_one(engine)
    engine.assignment_started(ticket.id, ticket.assignment_id)
    engine.report(ticket.id, ticket.assignment_id, Report("ready", "done"))
    ended = engine.store.get(Ticket, ticket.id)
    assert ended.state == "ended" and ended.outcome == "completed"
    assert engine.intake(make_trigger(text="one more thing", addressed="")) == "delivered"
    resumed = engine.store.get(Ticket, ticket.id)
    assert resumed.live and resumed.assignment_id != ticket.assignment_id
    assert any(i.kind == "resume" for i in resumed.inputs)


def test_bot_message_does_not_resume_an_ended_ticket(engine):
    _, ticket = accept_one(engine)
    engine.report(ticket.id, ticket.assignment_id, Report("ready", "done"))
    engine.intake(make_trigger(text="checks passed", actor="ci[bot]", bot=True, addressed=""))
    assert engine.store.get(Ticket, ticket.id).state == "ended"


def test_resume_is_refused_when_the_mandate_no_longer_holds(engine):
    _, ticket = accept_one(engine)
    engine.report(ticket.id, ticket.assignment_id, Report("ready", "done"))
    engine.set_policy(make_policy(allowed=("bob",)))
    result = engine.intake(make_trigger(text="please continue", addressed=""))
    assert result == "queued"  # not delivered; a non-listed follow-up goes to Eval as an inferred candidate
    assert engine.store.get(Ticket, ticket.id).state == "ended"


def test_stopped_ticket_receives_nothing_and_new_requests_go_to_eval(engine):
    _, ticket = accept_one(engine)
    assert engine.stop(ticket.id)
    assert engine.intake(make_trigger(text="@agent do it again")) == "queued"
    assert engine.store.get(Ticket, ticket.id).inputs[-1].trigger_id != "x"
    assert len(engine.store.get(Ticket, ticket.id).inputs) == 1


def test_hand_back_sends_the_message_to_eval_with_the_reason(engine):
    _, ticket = accept_one(engine)
    follow_up = make_trigger(text="and rewrite the whole docs site", addressed="")
    engine.intake(follow_up)
    assert engine.hand_back(ticket.id, follow_up.id, "beyond the ticket's scope") == ""
    [candidate] = engine.start_evals()
    assert candidate.handbacks == {follow_up.id: ["beyond the ticket's scope"]}
    assert candidate.eval.strength == "explicit"


def test_hand_back_accepts_the_id_of_the_input_card(engine):
    _, ticket = accept_one(engine)
    follow_up = make_trigger(text="also add a CLI", addressed="")
    engine.intake(follow_up)
    card_id = engine.store.get(Ticket, ticket.id).inputs[-1].id
    assert card_id != follow_up.id
    assert engine.hand_back(ticket.id, card_id, "a separate request") == ""
    [candidate] = engine.start_evals()
    assert candidate.handbacks == {follow_up.id: ["a separate request"]}


def test_hand_back_of_a_message_the_ticket_does_not_own_is_refused(engine):
    _, ticket = accept_one(engine)
    assert "does not own" in engine.hand_back(ticket.id, "github:comment:unknown", "x")


def test_note_reaches_the_worker_as_an_input_and_grants_nothing(engine):
    _, ticket = accept_one(engine)
    assert engine.note(ticket.id, "Ticket add-cli also edits README.md; expect a conflict.") == ""
    stored = engine.store.get(Ticket, ticket.id)
    assert stored.inputs[-1].kind == "orchestrator" and "README.md" in stored.inputs[-1].text
    assert stored.mandate == ticket.mandate
    assert engine.store.events(limit=1)[0]["type"] == "ticket.noted"


def test_status_request_is_not_queued_twice(engine):
    _, ticket = accept_one(engine)
    assert engine.note(ticket.id, "", kind="status") == ""
    assert "already waiting" in engine.note(ticket.id, "", kind="status")
    assert "Call `report`" in engine.store.get(Ticket, ticket.id).inputs[-1].text


def test_note_is_refused_for_ended_tickets_and_empty_text(engine):
    _, ticket = accept_one(engine)
    assert engine.note(ticket.id, "   ") == "the note is empty"
    assert engine.note(ticket.id, "x", kind="order") == "unknown note kind 'order'"
    engine.stop(ticket.id)
    assert engine.note(ticket.id, "too late") == "the ticket is not open"


def test_unread_follow_ups_are_released_to_eval_when_the_ticket_ends(engine):
    _, ticket = accept_one(engine)
    follow_up = make_trigger(text="also check the changelog", addressed="")
    engine.intake(follow_up)
    engine.report(ticket.id, ticket.assignment_id, Report("dropped", "nothing to do"))
    assert follow_up.id in engine.store.get(Thread, "github:o/r#1").queued


# -- stop and attach decisions ---------------------------------------------------------------------------------


def test_explicit_stop_decision_ends_the_ticket(engine):
    _, ticket = accept_one(engine)
    stop = make_trigger(text="@agent stop, not needed anymore")
    engine.intake(stop)
    engine.hand_back(ticket.id, stop.id, "asks to stop")
    [candidate] = engine.start_evals()
    decision = engine.decide(candidate.eval.id, Outcome("stop", "person asked to stop", ticket_id=ticket.id))
    assert decision.kind == "stop"
    stopped = engine.store.get(Ticket, ticket.id)
    assert stopped.stopped and stopped.ended_by == "person"
    assert (ticket.id, ticket.assignment_id) in engine.take_stopped_assignments()


def test_inferred_stop_is_refused(engine):
    _, ticket = accept_one(engine)
    stop = make_trigger(text="stop it", actor="bob", addressed="")
    assert engine.intake(stop) == "delivered"
    engine.hand_back(ticket.id, stop.id, "asks to stop")
    [candidate] = engine.start_evals()
    assert candidate.eval.strength == "inferred"
    decision = engine.decide(candidate.eval.id, Outcome("stop", "x", ticket_id=ticket.id))
    assert decision.kind == "ignore"
    assert engine.store.get(Ticket, ticket.id).live


def test_attach_subscribes_an_existing_ticket_to_another_thread(engine):
    _, ticket = accept_one(engine)
    other = make_trigger(text="@agent please include this in your PR for #1", thread="github:o/r#2")
    engine.intake(other)
    [candidate] = engine.start_evals()
    decision = engine.decide(candidate.eval.id, Outcome("attach", "points at #1", ticket_id=ticket.id))
    assert decision.kind == "attach"
    assert engine.store.get(Thread, "github:o/r#2").subscription(ticket.id) is not None
    assert any(i.trigger_id == other.id and i.owned for i in engine.store.get(Ticket, ticket.id).inputs)


# -- eval timing and policy changes ----------------------------------------------------------------------------


def test_expired_eval_is_retried_twice_then_recorded_unavailable(engine, clock):
    engine.intake(make_trigger())
    for _ in range(2):
        assert engine.start_evals()
        clock.advance(601)
        assert engine.expire_evals() == []
    assert engine.start_evals()
    clock.advance(601)
    [decision] = engine.expire_evals()
    assert decision.kind == "unavailable"


def test_policy_change_discards_running_eval_and_requeues_it(engine):
    engine.intake(make_trigger())
    [candidate] = engine.start_evals()
    engine.set_policy(make_policy(mode="auto"))
    assert engine.decide(candidate.eval.id, ACCEPT) is None
    [again] = engine.start_evals()
    assert again.eval.policy_version == engine.policy.version


def test_policy_change_notifies_affected_live_tickets(engine):
    engine.set_policy(make_policy(mode="auto", rules=[Rule("typos", "Fix typos", "Fix obvious typos.")]))
    engine.intake(make_trigger(text="typo", addressed="", observed=True))
    [candidate] = engine.start_evals()
    decision = engine.decide(candidate.eval.id, Outcome("accept", "typo", rules=("typos",)))
    engine.set_policy(make_policy(mode="observe", rules=[Rule("typos", "Fix typos", "Only in README.")]))
    notices = [i for i in engine.store.get(Ticket, decision.ticket_id).inputs if i.kind == "policy"]
    assert len(notices) == 1
    assert "Only in README." in notices[0].text and "no longer accepted" in notices[0].text


# -- execution -------------------------------------------------------------------------------------------------


def test_launch_start_and_delivery_flow(engine):
    _, ticket = accept_one(engine)
    [(launch, assignment)] = engine.pending_launches()
    assert launch.id == ticket.id
    engine.assignment_started(ticket.id, assignment.id)
    assert engine.pending_launches() == []
    engine.inputs_read(ticket.id, [i.id for i in ticket.inputs])
    assert engine.deliverable() == []
    engine.intake(make_trigger(text="more", addressed=""))
    assert [t.id for t in engine.deliverable()] == [ticket.id]


def test_blocked_ticket_holds_inputs_until_a_person_writes(engine):
    _, ticket = accept_one(engine)
    engine.assignment_started(ticket.id, ticket.assignment_id)
    engine.inputs_read(ticket.id, [i.id for i in ticket.inputs])
    engine.report(ticket.id, ticket.assignment_id, Report("blocked", "no access to the repo"))
    engine.intake(make_trigger(text="CI result", actor="ci[bot]", bot=True, addressed=""))
    assert engine.deliverable() == []
    engine.intake(make_trigger(text="I granted access", addressed=""))
    assert [t.id for t in engine.deliverable()] == [ticket.id]


def test_retry_starts_a_new_assignment_for_a_failed_ticket(engine):
    _, ticket = accept_one(engine)
    engine.assignment_started(ticket.id, ticket.assignment_id)
    assert not engine.retry(ticket.id)
    engine.report(ticket.id, ticket.assignment_id, Report("failed", "model unavailable"))
    assert engine.retry(ticket.id)
    retried = engine.store.get(Ticket, ticket.id)
    assert retried.assignment_id != ticket.assignment_id
    assert engine.store.get(Assignment, ticket.assignment_id).end_reason == "retry"


def test_report_from_a_stale_assignment_is_ignored(engine):
    _, ticket = accept_one(engine)
    engine.report(ticket.id, "as_old", Report("ready", "done"))
    assert engine.store.get(Ticket, ticket.id).reports == []


def test_ready_with_pull_request_waits_for_the_pull_request_to_close(engine):
    _, ticket = accept_one(engine)
    engine.assignment_started(ticket.id, ticket.assignment_id)
    engine.report(ticket.id, ticket.assignment_id,
                  Report("ready", "PR open", pull_request="https://github.com/o/r/pull/7"))
    waiting = engine.store.get(Ticket, ticket.id)
    assert waiting.live and waiting.opened_pull_requests == ["github:o/r#7"]
    engine.intake(make_trigger(text="", actor="", addressed="", kind="pr_closed", thread="github:o/r#7",
                               meta={"merged": True}))
    done = engine.store.get(Ticket, ticket.id)
    assert done.state == "ended" and done.outcome == "merged"


def test_reply_is_queued_and_long_replies_are_refused(engine):
    _, ticket = accept_one(engine)
    assert engine.reply(ticket.id, "Fixed in [PR #7](https://example).", "result") == ""
    assert messages(engine)[0]["text"].startswith("Fixed in")
    assert "limit" in engine.reply(ticket.id, "word " * 200, "result")
    assert "purpose" in engine.reply(ticket.id, "hi", "chit-chat")


def test_follow_adds_a_context_only_subscription(engine):
    _, ticket = accept_one(engine)
    assert engine.follow(ticket.id, "#5") == "github:o/r#5"
    assert engine.store.get(Thread, "github:o/r#5").subscription(ticket.id).context_only
    assert engine.intake(make_trigger(text="unrelated chat", thread="github:o/r#5", addressed="")) == "filtered"


def test_worker_cannot_unsubscribe_from_its_origin_thread(engine):
    _, ticket = accept_one(engine)
    assert "cannot" in engine.unsubscribe(ticket.id, "github:o/r#1")
    engine.follow(ticket.id, "#5")
    assert engine.unsubscribe(ticket.id, "github:o/r#5") == ""


def test_recover_restarts_busy_assignments_and_requeues_evals(engine):
    _, ticket = accept_one(engine)
    engine.assignment_started(ticket.id, ticket.assignment_id)
    engine.intake(make_trigger(text="@agent other", thread="github:o/r#3"))
    engine.start_evals()
    engine.recover()
    recovered = engine.store.get(Ticket, ticket.id)
    assert recovered.assignment_id != ticket.assignment_id
    assert recovered.inputs[-1].kind == "recovery"
    assert engine.store.get(Thread, "github:o/r#3").queued


def test_pause_stops_evals_launches_and_deliveries(engine):
    engine.set_paused(True)
    engine.intake(make_trigger())
    assert engine.start_evals() == []
    engine.set_paused(False)
    assert len(engine.start_evals()) == 1


def test_decisions_are_stored(engine):
    decision, _ = accept_one(engine)
    assert engine.store.get(Decision, decision.id).ticket_id == decision.ticket_id


def test_listened_threads_include_live_and_recently_ended_tickets(engine, clock):
    _, ticket = accept_one(engine)
    engine.follow(ticket.id, "#5")
    assert engine.listened_threads() == {"github:o/r#1", "github:o/r#5"}
    engine.report(ticket.id, ticket.assignment_id, Report("ready", "done"))
    clock.advance(13 * 86400)
    assert "github:o/r#1" in engine.listened_threads()
    clock.advance(2 * 86400)
    assert engine.listened_threads() == set()


def test_stopped_tickets_are_not_listened_to(engine):
    _, ticket = accept_one(engine)
    engine.stop(ticket.id)
    assert engine.listened_threads() == set()
