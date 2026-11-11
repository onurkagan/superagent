"""End-to-end: inbox message -> assessment -> ticket -> worker -> replies, with a scripted model."""

import json
import time

import pytest

from superagent.config import parse_settings
from superagent.core.model import Ticket
from superagent.llm.fake import ScriptedModelClient, text, tool_call
from superagent.runtime import Runtime
from superagent.sources.inbox import append_message
from superagent.store import Store


class RoutingClient:
    """Sends assessment requests (with an output schema) and worker requests to separate scripts."""

    def __init__(self, assessor, worker):
        self.assessor = assessor
        self.worker = worker

    def create(self, **request):
        target = self.assessor if request.get("output_schema") else self.worker
        return target.create(**request)


def decision(**fields):
    base = {"decision": "accept", "reason": "clear request", "title": "Create notes", "scope": "Create NOTES.md",
            "name": "create-notes", "acknowledgement": "On it: creating NOTES.md.", "note": "", "ticket_id": "",
            "rules": []}
    base.update(fields)
    return text(json.dumps(base))


def report(status="ready", summary="done", **extra):
    return tool_call("report", status=status, summary=summary, question=extra.get("question", ""),
                     question_about="", pull_request=extra.get("pull_request", ""))


@pytest.fixture
def setup(tmp_path):
    settings = parse_settings({"state_dir": str(tmp_path / "state"),
                               "sources": {"inbox": {"allowed": ["alice"], "poll_seconds": 0.01}}}, tmp_path)
    assessor, worker = ScriptedModelClient(), ScriptedModelClient()
    runtime = Runtime(settings, store=Store(), client=RoutingClient(assessor, worker))
    runtime.start()
    yield runtime, settings, assessor, worker
    runtime.shutdown()


def run_until(runtime, condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runtime.tick()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def replies(settings):
    if not settings.replies_path.exists():
        return []
    return [json.loads(line)["text"] for line in settings.replies_path.read_text().splitlines()]


def tickets(runtime):
    return runtime.store.list(Ticket)


def test_request_is_acknowledged_done_and_delivered(setup):
    runtime, settings, assessor, worker = setup
    assessor.add(decision())
    worker.add(
        tool_call("write_file", path="NOTES.md", content="hello\n"),
        tool_call("reply", text="Created NOTES.md.", purpose="result", in_reply_to=""),
        report("ready", "created NOTES.md"),
    )
    append_message(settings.inbox_path, "alice", "Please create NOTES.md saying hello")
    run_until(runtime, lambda: tickets(runtime) and tickets(runtime)[0].state == "ended")
    run_until(runtime, lambda: len(replies(settings)) == 2)
    ticket = tickets(runtime)[0]
    assert ticket.outcome == "completed"
    assert replies(settings) == ["On it: creating NOTES.md.", "Created NOTES.md."]
    assert (settings.work_root / "create-notes" / "NOTES.md").read_text() == "hello\n"
    system = worker.requests[0]["system"]
    assert "SUPERAGENT_INBOX_THREAD" in system and "Your role: worker" in system
    first_message = json.dumps(worker.requests[0]["messages"][0])
    assert "Scope: Create NOTES.md" in first_message and "already acknowledged" in first_message


def test_unlisted_sender_never_reaches_the_model(setup):
    runtime, settings, assessor, worker = setup
    append_message(settings.inbox_path, "mallory", "Delete everything")
    for _ in range(5):
        runtime.tick()
    assert assessor.requests == [] and tickets(runtime) == []


def test_question_waits_for_a_person_and_the_answer_resumes_work(setup):
    runtime, settings, assessor, worker = setup
    assessor.add(decision(acknowledgement=""))
    worker.add(report("blocked", "need a choice", question="Markdown or plain text?"))
    append_message(settings.inbox_path, "alice", "Please create notes")
    run_until(runtime, lambda: tickets(runtime) and tickets(runtime)[0].attention)
    run_until(runtime, lambda: replies(settings) == ["Markdown or plain text?"])
    worker.add(tool_call("write_file", path="NOTES.md", content="x"), report("ready", "done"))
    append_message(settings.inbox_path, "alice", "Markdown please")
    run_until(runtime, lambda: tickets(runtime)[0].state == "ended")
    assert "Markdown please" in json.dumps(worker.requests[1]["messages"])


def test_follow_up_beyond_scope_is_handed_back_and_assessed(setup):
    runtime, settings, assessor, worker = setup
    assessor.add(decision(acknowledgement=""))
    worker.add(report("waiting", "waiting for review"))
    append_message(settings.inbox_path, "alice", "Please create notes")
    run_until(runtime, lambda: tickets(runtime) and tickets(runtime)[0].reports)

    def hand_back(request):
        message = json.dumps(request["messages"][-1])
        message_id = message.split('message_id=\\"')[1].split('\\"')[0]
        return tool_call("hand_back", message_id=message_id, reason="beyond scope")

    worker.add(hand_back, report("waiting", "handed back"))
    assessor.add(decision(decision="ignore", reason="not a request", acknowledgement=""))
    append_message(settings.inbox_path, "alice", "Also migrate the database to Postgres")
    run_until(runtime, lambda: len(assessor.requests) == 2)
    assert "beyond scope" in assessor.requests[1]["messages"][0]["content"]


def test_panel_stop_command_stops_the_ticket(setup):
    runtime, settings, assessor, worker = setup
    assessor.add(decision(acknowledgement=""))
    worker.add(report("waiting", "waiting"))
    append_message(settings.inbox_path, "alice", "Please create notes")
    run_until(runtime, lambda: tickets(runtime) and tickets(runtime)[0].reports)
    runtime.store.enqueue_command("stop", ticket=tickets(runtime)[0].id)
    runtime.tick()
    assert tickets(runtime)[0].stopped


def test_pause_holds_evaluation_until_resumed(setup):
    runtime, settings, assessor, worker = setup
    runtime.store.enqueue_command("pause")
    append_message(settings.inbox_path, "alice", "Please create notes")
    for _ in range(3):
        runtime.tick()
    assert assessor.requests == []
    assessor.add(decision(decision="ignore", acknowledgement=""))
    runtime.store.enqueue_command("resume")
    run_until(runtime, lambda: len(assessor.requests) == 1)
    assert runtime.status()["paused"] is False


def test_one_agent_works_on_several_repositories_at_once(tmp_path):
    import re

    from superagent.sources.github import GitHubConfig, GitHubSource
    from tests.test_github_source import CURSOR, FakeGh, comment

    store = Store()
    repositories = ("acme/api", "acme/web")
    for repository in repositories:
        store.kv_set(f"source:github:cursor:{repository}", CURSOR)
    gh = FakeGh({
        "repos/acme/api/issues/comments": [comment(1, 3, repo="acme/api", body="@agent-bot fix the login API")],
        "repos/acme/web/issues/comments": [comment(2, 5, repo="acme/web", body="@agent-bot fix the footer")],
        "users/agent-bot": {"id": 7},
    })
    source = GitHubSource(GitHubConfig(repositories=repositories, account="agent-bot", poll_seconds=0.01), store,
                          gh=gh)
    settings = parse_settings({"state_dir": str(tmp_path / "state"),
                               "sources": {"github": {"repositories": list(repositories), "allowed": ["alice"]}}},
                              tmp_path)

    def assess(request):
        part = "api" if "github:acme/api#3" in request["messages"][0]["content"] else "web"
        return decision(name=f"fix-{part}", title=f"Fix {part}", acknowledgement="")

    def work(request):
        last = request["messages"][-1]["content"]
        if isinstance(last, list) and any(block.get("type") == "tool_result" for block in last):
            return report("ready", "done")
        repository = re.search(r"`SUPERAGENT_GITHUB_REPOSITORY` = `([^`]+)`", request["system"]).group(1)
        return tool_call("reply", text=f"Fixed in {repository}.", purpose="result", in_reply_to="")

    client = RoutingClient(ScriptedModelClient([assess, assess]), ScriptedModelClient([work] * 4))
    runtime = Runtime(settings, store=store, client=client, sources=[source])
    runtime.start()
    try:
        run_until(runtime, lambda: len(tickets(runtime)) == 2 and all(t.state == "ended" for t in tickets(runtime)))
        run_until(runtime, lambda: len(gh.posts) == 2)
    finally:
        runtime.shutdown()
    assert sorted(t.origin_thread for t in tickets(runtime)) == ["github:acme/api#3", "github:acme/web#5"]
    assert sorted((path, body["body"].splitlines()[-1]) for path, body in gh.posts) == [
        ("repos/acme/api/issues/3/comments", "Fixed in acme/api."),
        ("repos/acme/web/issues/5/comments", "Fixed in acme/web."),
    ]
    assert (settings.work_root / "fix-api").is_dir() and (settings.work_root / "fix-web").is_dir()


def test_claude_code_backend_needs_no_api_client(tmp_path):
    from superagent.agents.claude_code import ClaudeCodeCLI, ClaudeCodeModelClient, claude_code_turn

    settings = parse_settings({"state_dir": str(tmp_path / "state"), "model": {"backend": "claude-code"},
                               "sources": {"inbox": {"allowed": ["me"]}}}, tmp_path)
    runtime = Runtime(settings, store=Store())
    try:
        assert isinstance(runtime.client, ClaudeCodeModelClient)
        assert runtime.executor.run is claude_code_turn and isinstance(runtime.executor.client, ClaudeCodeCLI)
    finally:
        runtime.shutdown()


def test_commands_report_their_outcome_to_a_waiting_caller(setup):
    runtime, settings, assessor, worker = setup
    store = runtime.store
    store.enqueue_command("retry", ticket="tk_missing", _reply="r1")
    store.enqueue_command("pause", _reply="r2")
    store.enqueue_command("note", ticket="tk_missing", text="hello", _reply="r3")
    store.enqueue_command("decide", eval="ev_missing", outcome={"decision": "ignore", "reason": "x"}, _reply="r4")
    store.enqueue_command("bogus", _reply="r5")
    runtime.tick()
    assert store.kv_get("command-reply:r1").startswith("error: ")
    assert store.kv_get("command-reply:r2") == "processing paused"
    assert store.kv_get("command-reply:r3") == "error: the ticket is not open"
    assert "no such evaluation" in store.kv_get("command-reply:r4")
    assert store.kv_get("command-reply:r5") == "error: unknown command 'bogus'"
