from superagent.core.model import Assignment, Mandate, Report, Ticket
from superagent.core.stage import ticket_stage


def ticket(**kw):
    t = Ticket(id="tk", name="n", title="t", decision_id="d", origin_thread="th", origin_trigger="tr",
               mandate=Mandate("p", "explicit", "allowlist", "s", "alice", "github"))
    for k, v in kw.items():
        setattr(t, k, v)
    return t


running = Assignment(id="as", ticket_id="tk", state="running")


def test_ended_ticket_shows_outcome():
    stage = ticket_stage(ticket(state="ended", outcome="merged"), None)
    assert (stage.group, stage.label) == ("ended", "Pull request merged")


def test_failed_delivery_wins_over_everything_else():
    t = ticket(delivery_failures=1, attention="Which branch?")
    assert ticket_stage(t, running).name == "delivery"


def test_question_waits_for_answer():
    assert ticket_stage(ticket(attention="Which branch?"), running).name == "answer"


def test_ready_report_awaits_review():
    stage = ticket_stage(ticket(reports=[Report("ready", "done")]), running)
    assert stage.name == "review" and stage.needs_person


def test_blocked_report_needs_a_person_until_a_person_writes():
    t = ticket(reports=[Report("blocked", "no access")])
    assert ticket_stage(t, running).name == "blocked"
    t.human_since_report = True
    assert ticket_stage(t, running).name == "working"


def test_no_assignment_or_pending_is_queued():
    assert ticket_stage(ticket(), None).name == "queued"
    assert ticket_stage(ticket(), Assignment(id="a", ticket_id="tk")).name == "queued"


def test_waiting_report_is_idle():
    assert ticket_stage(ticket(reports=[Report("waiting", "ci running")]), running).group == "idle"


def test_otherwise_working():
    assert ticket_stage(ticket(reports=[Report("progress", "half way")]), running).name == "working"
