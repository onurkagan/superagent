"""End-to-end: the orchestrator routes, answers the operator, coordinates workers, and falls back on the assessor."""

import json
import re
import time

import pytest

from superagent.config import parse_settings
from superagent.core.model import Ticket
from superagent.llm.client import ModelError
from superagent.llm.fake import ScriptedModelClient, text, tool_call, tool_calls
from superagent.orchestrator import CHAT_KEY, STATE_KEY, chat_log
from superagent.runtime import Runtime
from superagent.sources.inbox import append_message
from superagent.store import Store


class Router:
    """Sends orchestrator, assessor and worker requests to separate scripts."""

    def __init__(self):
        self.orchestrator, self.assessor, self.worker = ScriptedModelClient(), ScriptedModelClient(), \
            ScriptedModelClient()

    def create(self, **request):
        names = {t["name"] for t in request.get("tools") or []}
        if "route" in names:
            return self.orchestrator.create(**request)
        if request.get("output_schema"):
            return self.assessor.create(**request)
        return self.worker.create(**request)


ACCEPT = {"decision": "accept", "reason": "clear request", "title": "Create notes", "scope": "Create NOTES.md",
          "name": "create-notes", "acknowledgement": "On it: creating NOTES.md.", "note": "", "ticket_id": "",
          "rules": []}


def update_text(request):
    content = request["messages"][-1]["content"]
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def route_accept(request):
    eval_id = re.search(r'<candidate eval="([^"]+)"', update_text(request)).group(1)
    return tool_calls(("route", {"eval_id": eval_id, **ACCEPT}))


def report(status="ready", summary="done"):
    return tool_call("report", status=status, summary=summary, question="", question_about="", pull_request="")


def make_runtime(tmp_path, **orchestrator):
    settings = parse_settings({"state_dir": str(tmp_path / "state"),
                               "orchestrator": {"enabled": True, "batch_seconds": 0, **orchestrator},
                               "sources": {"inbox": {"allowed": ["alice"], "poll_seconds": 0.01}}}, tmp_path)
    client = Router()
    runtime = Runtime(settings, store=Store(), client=client)
    runtime.start()
    return runtime, settings, client


@pytest.fixture
def setup(tmp_path):
    runtime, settings, client = make_runtime(tmp_path)
    yield runtime, settings, client
    runtime.shutdown()


def run_until(runtime, condition, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runtime.tick()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def orchestrator_said(runtime, fragment):
    return any(e["role"] == "orchestrator" and fragment in e["text"] for e in chat_log(runtime.store))


def test_the_orchestrator_routes_a_request_and_hears_the_report(setup):
    runtime, settings, client = setup
    client.orchestrator.add(route_accept, text("Accepted create-notes."), text("Nothing to do."))
    client.worker.add(tool_call("write_file", path="NOTES.md", content="hi\n"), report("ready", "created NOTES.md"))
    append_message(settings.inbox_path, "alice", "Please create NOTES.md")
    run_until(runtime, lambda: orchestrator_said(runtime, "Nothing to do."))
    [ticket] = runtime.store.list(Ticket)
    assert ticket.name == "create-notes" and ticket.outcome == "completed"
    assert client.assessor.requests == []
    first, *_, last = client.orchestrator.requests
    assert "Your role: orchestrator" in first["system"] and "<candidate eval=" in update_text(first)
    assert "create-notes" in update_text(last) and "reported ready: created NOTES.md" in update_text(last)
    assert runtime.store.kv_get(STATE_KEY)["turns"] == 2


def test_the_operator_chats_with_the_orchestrator(setup):
    runtime, settings, client = setup
    client.orchestrator.add(tool_call("list_tickets", which="open"), text("Nothing is open right now."))
    runtime.store.enqueue_command("chat", text="What is open?", id="ch_1", _reply="r1")
    run_until(runtime, lambda: orchestrator_said(runtime, "Nothing is open"))
    operator, answer = chat_log(runtime.store)
    assert operator == {**operator, "id": "ch_1", "role": "operator", "text": "What is open?"}
    assert answer["reply_to"] == ["ch_1"] and not answer["error"]
    assert runtime.store.kv_get("command-reply:r1") == "ch_1"
    assert '<operator id="ch_1"' in update_text(client.orchestrator.requests[0])


def test_a_report_wakes_the_orchestrator_which_notes_another_worker(setup):
    runtime, settings, client = setup
    client.orchestrator.add(route_accept, text("Accepted."))
    client.worker.add(report("waiting", "waiting for review"))
    append_message(settings.inbox_path, "alice", "Please create NOTES.md")
    run_until(runtime, lambda: orchestrator_said(runtime, "Accepted."))
    client.orchestrator.add(tool_call("note", ticket="create-notes", text="Ticket add-readme also edits NOTES.md."),
                            text("Told create-notes about add-readme."))
    client.worker.add(report("waiting", "noted the overlap"))
    run_until(runtime, lambda: orchestrator_said(runtime, "Told create-notes"))
    run_until(runtime, lambda: any(i.kind == "orchestrator" and i.read
                                   for i in runtime.store.list(Ticket)[0].inputs))
    worker_turn = client.worker.requests[-1]["messages"]
    cards = [b["text"] for m in worker_turn if isinstance(m["content"], list) for b in m["content"]
             if b.get("type") == "text"]
    assert any('kind="orchestrator"' in c and "add-readme also edits NOTES.md" in c for c in cards)


def test_a_failed_turn_hands_its_candidates_to_the_assessor(setup):
    runtime, settings, client = setup
    client.orchestrator.add(ModelError("not logged in"))
    client.assessor.add(text(json.dumps(ACCEPT)))
    client.worker.add(report("waiting", "started"))
    client.orchestrator.add(text("Nothing to do."), text("Nothing to do."))
    append_message(settings.inbox_path, "alice", "Please create NOTES.md")
    run_until(runtime, lambda: runtime.store.list(Ticket))
    assert len(client.assessor.requests) == 1
    failed = [e for e in chat_log(runtime.store) if e.get("error")]
    assert failed and "not logged in" in failed[0]["text"]


def test_route_false_keeps_the_assessor_and_chat_needs_the_orchestrator(tmp_path):
    runtime, settings, client = make_runtime(tmp_path, route=False)
    client.assessor.add(text(json.dumps({**ACCEPT, "decision": "ignore", "reason": "chat"})))
    client.orchestrator.add(text("Nothing to do."))
    append_message(settings.inbox_path, "alice", "thanks!")
    run_until(runtime, lambda: len(client.assessor.requests) == 1)
    runtime.shutdown()
    off = parse_settings({"state_dir": str(tmp_path / "off"), "sources": {"inbox": {"allowed": ["a"]}}}, tmp_path)
    plain = Runtime(off, store=Store(), client=Router())
    plain.store.enqueue_command("chat", text="hi", _reply="r")
    plain.tick()
    assert "orchestrator is off" in plain.store.kv_get("command-reply:r")
    assert plain.store.kv_get(CHAT_KEY) is None
