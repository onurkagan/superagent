from superagent.agents.orchestrator_tools import ORCHESTRATOR_TOOLS, WORKERS_KEY, OrchestratorTools
from superagent.core.engine import Outcome
from superagent.core.model import Report, Ticket
from tests.conftest import make_trigger


class Recorder:
    """Stands in for the command channel: records commands and answers with a fixed outcome."""

    def __init__(self, outcome="done"):
        self.calls = []
        self.outcome = outcome

    def __call__(self, store, kind, timeout, **args):
        self.calls.append((kind, args))
        return self.outcome


def accept(engine, text="@agent please add a CLI", name="add-cli", thread="github:o/r#1"):
    assert engine.intake(make_trigger(text=text, thread=thread)) == "queued"
    [candidate] = engine.start_evals()
    decision = engine.decide(candidate.eval.id, Outcome("accept", "clear", title=f"Do {name}", scope=text, name=name,
                                                        acknowledgement="On it."))
    return engine.store.get(Ticket, decision.ticket_id)


def test_definitions_are_strict_and_unique():
    names = [t["name"] for t in ORCHESTRATOR_TOOLS]
    assert len(names) == len(set(names))
    for tool in ORCHESTRATOR_TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] and schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"])


def test_list_tickets_shows_stage_running_workers_and_reports(engine):
    first = accept(engine)
    second = accept(engine, "@agent add a readability module", "add-readability", "github:o/r#2")
    engine.report(second.id, second.assignment_id, Report("ready", "Opened PR #3 with the module.",
                                                       pull_request="https://github.com/o/r/pull/3"))
    engine.store.kv_set(WORKERS_KEY, {"busy_tickets": [first.id], "running": 1, "concurrency": 3, "queued": 0})
    text, error = OrchestratorTools(engine.store, caller=Recorder()).execute("list_tickets", {"which": "open"})
    assert not error
    assert "2 open tickets. Worker sessions running: 1 of 3" in text
    assert f"{first.id} [add-cli] Queued · worker running now" in text
    assert "latest report (ready" in text and "Opened PR #3" in text and "pull requests: github:o/r#3" in text


def test_read_ticket_by_name_and_read_conversation(engine):
    ticket = accept(engine)
    tools = OrchestratorTools(engine.store, caller=Recorder())
    text, error = tools.execute("read_ticket", {"ticket": "add-cli"})
    assert not error and ticket.id in text and "Mandate: @agent please add a CLI" in text
    assert "acknowledgement (pending)" in text
    text, error = tools.execute("read_conversation", {"thread": "github:o/r#1"})
    assert not error and "Followed by: add-cli" in text and "[decided" in text
    assert tools.execute("read_ticket", {"ticket": "nope"})[1]
    assert tools.execute("read_conversation", {"thread": "nope"})[1]


def test_acting_tools_queue_engine_commands(engine):
    ticket = accept(engine)
    recorder = Recorder("noted")
    tools = OrchestratorTools(engine.store, caller=recorder)
    text, error = tools.execute("note", {"ticket": "add-cli", "text": "Ticket add-docs also edits README.md."})
    assert not error and text.startswith("noted; its worker starts a turn")
    tools.execute("request_status", {"ticket": ticket.id})
    tools.execute("retry", {"ticket": ticket.id})
    tools.execute("stop_ticket", {"ticket": ticket.id, "reason": "duplicate of add-cli-2"})
    tools.execute("reevaluate", {"message_id": "github:comment:9"})
    tools.execute("notify", {"ticket": ticket.id, "text": "Please approve the deploy."})
    tools.execute("set_processing", {"paused": True})
    tools.execute("route", {"eval_id": "ev_1", "decision": "ignore", "reason": "chat", "title": "", "scope": "",
                            "name": "", "acknowledgement": "", "note": "", "ticket_id": "", "rules": []})
    kinds = [(kind, args.get("note_kind") or args.get("by") or "") for kind, args in recorder.calls]
    assert kinds == [("note", "orchestrator"), ("note", "status"), ("retry", ""), ("stop", "orchestrator"),
                     ("reevaluate", ""), ("notify", ""), ("pause", ""), ("decide", "")]
    assert recorder.calls[-1][1] == {"eval": "ev_1", "outcome": {
        "decision": "ignore", "reason": "chat", "title": "", "scope": "", "name": "", "acknowledgement": "",
        "note": "", "ticket_id": "", "rules": []}}


def test_refusals_and_timeouts_are_errors(engine):
    accept(engine)
    tools = OrchestratorTools(engine.store, caller=Recorder("error: the ticket is not open"))
    assert tools.execute("retry", {"ticket": "add-cli"}) == ("error: the ticket is not open", True)
    assert tools.execute("note", {"ticket": "add-cli", "text": "word " * 200})[1]
    assert tools.execute("stop_ticket", {"ticket": "add-cli", "reason": ""})[1]
    assert tools.execute("bogus", {}) == ("unknown tool 'bogus'", True)
    late = OrchestratorTools(engine.store, caller=lambda *a, **k: None)
    text, error = late.execute("retry", {"ticket": "add-cli"})
    assert error and "did not apply the command in time" in text
