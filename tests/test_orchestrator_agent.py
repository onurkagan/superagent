import json
import stat
import sys

import pytest

from superagent.agents.claude_code import ClaudeCodeCLI
from superagent.agents.orchestrator import (
    RESET_NOTE,
    OrchestratorSpec,
    api_turn,
    claude_code_turn,
    orchestrator_args,
)
from superagent.agents.orchestrator_tools import OrchestratorTools
from superagent.agents.worker import Session
from superagent.llm.client import ModelError, TransientModelError
from superagent.llm.fake import ScriptedModelClient, text, tool_call
from superagent.store import Store


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, store, kind, timeout, **args):
        self.calls.append((kind, args))
        return "noted"


@pytest.fixture
def spec(tmp_path):
    return OrchestratorSpec(system="You are the orchestrator.", model="claude-opus-5-5", effort="medium",
                            session_path=tmp_path / "orchestrator.json", directory=tmp_path / "orchestrator",
                            db_path=str(tmp_path / "superagent.db"))


def test_api_turn_runs_tools_until_the_model_answers(spec):
    client = ScriptedModelClient([tool_call("list_tickets", which="open"), text("Two tickets are open.")])
    result = api_turn(spec, client, "<operator>What is open?</operator>", OrchestratorTools(Store(), caller=Recorder()))
    assert result.text == "Two tickets are open." and not result.error
    first, second = client.requests
    assert [t["name"] for t in first["tools"]][:3] == ["list_tickets", "read_ticket", "read_conversation"]
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "0 open tickets" in second["messages"][-1]["content"][0]["content"]


def test_api_turn_continues_the_same_conversation_and_resets_a_long_one(spec):
    tools = OrchestratorTools(Store(), caller=Recorder())
    client = ScriptedModelClient([text("ok"), text("ok again")])
    api_turn(spec, client, "first update", tools)
    api_turn(spec, client, "second update", tools)
    assert len(client.requests[1]["messages"]) == 3
    spec.max_history_chars = 10
    client.add(text("fresh"))
    api_turn(spec, client, "third update", tools)
    messages = client.requests[2]["messages"]
    assert len(messages) == 1 and messages[0]["content"][0]["text"].startswith(RESET_NOTE)


def test_api_turn_answers_a_call_left_unanswered_by_a_crash(spec):
    session = Session(spec.session_path)
    session.messages = [{"role": "user", "content": "update"},
                        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "retry",
                                                           "input": {"ticket": "x"}}]}]
    session.save()
    client = ScriptedModelClient([text("ok")])
    api_turn(spec, client, "next update", OrchestratorTools(Store(), caller=Recorder()))
    repaired = client.requests[0]["messages"][2]["content"]
    assert repaired[0]["type"] == "tool_result" and repaired[0]["is_error"]
    assert repaired[-1] == {"type": "text", "text": "next update"}


def test_api_turn_reports_model_failures(spec):
    tools = OrchestratorTools(Store(), caller=Recorder())
    assert "failed" in api_turn(spec, ScriptedModelClient([ModelError("bad key")]), "u", tools).error
    flaky = ScriptedModelClient([TransientModelError("busy"), text("fine")])
    assert api_turn(spec, flaky, "u", tools, sleep=lambda _: None).text == "fine"


FAKE = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKECC_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd()}) + "\n")
mode = os.environ.get("FAKECC_MODE", "answer")
if mode == "lost" and "--resume" in args:
    print(json.dumps({"type": "result", "is_error": True, "result": "No conversation found with session ID: x"}))
    sys.exit(1)
if mode == "overloaded":
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 529, "result": "Overloaded"}))
    sys.exit(1)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
print(json.dumps({"type": "result", "is_error": False, "result": "Routed one candidate.", "session_id": sid}))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKECC_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return ClaudeCodeCLI(str(path)), calls, monkeypatch


def test_orchestrator_args_give_only_the_orchestrator_tools(spec):
    args = orchestrator_args(spec, "sid", resume=False)
    assert args[args.index("--tools") + 1] == ""
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "mcp__superagent__route" in allowed and "mcp__superagent__reply" not in allowed and "Bash" not in allowed
    server = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["superagent"]
    assert server["env"]["SUPERAGENT_ROLE"] == "orchestrator" and server["env"]["SUPERAGENT_DB"] == spec.db_path
    assert args[-2:] == ["--session-id", "sid"] and orchestrator_args(spec, "sid", True)[-2:] == ["--resume", "sid"]


def test_claude_code_turn_starts_then_resumes_one_session(spec, fake):
    cli, calls, _ = fake
    assert claude_code_turn(spec, cli, "update one").text == "Routed one candidate."
    assert claude_code_turn(spec, cli, "update two").text == "Routed one candidate."
    first, second = calls()
    session_id = first["args"][first["args"].index("--session-id") + 1]
    assert second["args"][-2:] == ["--resume", session_id]
    assert second["prompt"] == "update two" and second["cwd"].endswith("orchestrator")


def test_claude_code_turn_starts_a_new_session_when_the_old_one_is_gone(spec, fake):
    cli, calls, monkeypatch = fake
    claude_code_turn(spec, cli, "one")
    monkeypatch.setenv("FAKECC_MODE", "lost")
    result = claude_code_turn(spec, cli, "two")
    assert result.text == "Routed one candidate."
    retry = calls()[-1]
    assert "--session-id" in retry["args"] and retry["prompt"].startswith(RESET_NOTE)


def test_claude_code_turn_reports_failures(spec, fake):
    cli, calls, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "overloaded")
    result = claude_code_turn(spec, cli, "u", sleep=lambda _: None)
    assert "Overloaded" in result.error and len(calls()) == 3
