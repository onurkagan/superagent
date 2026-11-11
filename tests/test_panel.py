import json
import urllib.error
import urllib.request

import pytest

from superagent.core.engine import Outcome
from superagent.core.model import Report, Ticket
from superagent.panel.data import PanelData
from superagent.panel.server import PanelServer
from tests.conftest import make_trigger


def accept(engine, text="@agent fix the typo", thread="github:issue:1"):
    engine.intake(make_trigger(text=text, thread=thread))
    [candidate] = engine.start_evals()
    return engine.decide(candidate.eval.id, Outcome("accept", "ok", title="Fix typo", acknowledgement="On it."))


def test_needs_person_lists_only_tickets_waiting_for_a_person(engine):
    first = accept(engine)
    second = accept(engine, "@agent update docs", thread="github:issue:2")
    assignment_id = engine.store.get(Ticket, first.ticket_id).assignment_id
    engine.report(first.ticket_id, assignment_id, Report("blocked", "need access", question="Can you grant access?"))
    data = PanelData(engine.store)
    needs = data.needs_person()
    assert [t["id"] for t in needs] == [first.ticket_id]
    assert needs[0]["stage"]["label"] == "Awaiting answer" and needs[0]["attention"] == "Can you grant access?"
    assert {t["id"] for t in data.tickets("open")} == {first.ticket_id, second.ticket_id}


def test_overview_counts_activity_and_detail_shows_history(engine):
    decision = accept(engine)
    data = PanelData(engine.store, runtime_status=lambda: {"policy": "p-x", "executor": {"running": 0},
                                                           "sources": {"github": {"ok": False, "detail": "down"}}})
    overview = data.overview()
    assert overview["activity"]["triggers"] == 1 and overview["activity"]["decisions_accept"] == 1
    assert overview["activity"]["tickets_created"] == 1
    assert overview["problems"][0]["kind"] == "source"
    detail = data.ticket(decision.ticket_id)
    assert detail["mandate"]["requester"] == "alice"
    assert detail["messages"][0]["kind"] == "acknowledgement"
    assert detail["inputs"][0]["text"] == "@agent fix the typo"
    assert data.ticket("missing") is None


def test_unavailable_decision_is_a_problem_until_requeued(engine, clock):
    engine.intake(make_trigger())
    for _ in range(3):
        engine.start_evals()
        clock.advance(601)
        engine.expire_evals()
    problems = PanelData(engine.store).problems()
    assert problems and problems[0]["kind"] == "eval"


def test_commands_are_validated_and_queued(engine):
    data = PanelData(engine.store)
    data.command("stop", ticket="tk_1")
    with pytest.raises(ValueError):
        data.command("explode")
    assert engine.store.take_commands() == [("stop", {"ticket": "tk_1"})]


def test_threads_show_triggers_with_dispositions(engine):
    engine.intake(make_trigger(actor="mallory"))
    [thread] = PanelData(engine.store).threads("all")
    assert thread["triggers"][0]["disposition"]["kind"] == "filtered"


@pytest.fixture
def server(engine):
    accept(engine)
    panel = PanelServer(PanelData(engine.store), port=0, token="secret")
    panel.start()
    yield panel, engine
    panel.stop()


def call(url, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_server_requires_the_token(server):
    panel, _ = server
    assert call(panel.url + "api/overview")[0] == 401
    status, body = call(panel.url + "api/overview", headers={"X-Superagent-Token": "secret"})
    assert status == 200 and json.loads(body)["open_tickets"] == 1


def test_server_serves_the_page_and_ticket_detail(server):
    panel, engine = server
    status, page = call(panel.url + "?token=secret")
    assert status == 200 and "<title>superagent</title>" in page
    [row] = json.loads(call(panel.url + "api/tickets?token=secret")[1])
    status, body = call(panel.url + f"api/tickets/{row['id']}?token=secret")
    assert status == 200 and json.loads(body)["id"] == row["id"]
    assert call(panel.url + "api/tickets/nope?token=secret")[0] == 404


def test_post_actions_need_the_panel_header(server):
    panel, engine = server
    headers = {"X-Superagent-Token": "secret"}
    assert call(panel.url + "api/pause", "POST", headers)[0] == 403
    status, _ = call(panel.url + "api/pause", "POST", {**headers, "X-Requested-By": "superagent-panel"})
    assert status == 202
    assert engine.store.take_commands() == [("pause", {})]


def test_non_local_binding_requires_a_token(engine):
    with pytest.raises(ValueError):
        PanelServer(PanelData(engine.store), host="0.0.0.0", port=0)
