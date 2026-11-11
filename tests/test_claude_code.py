import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from superagent.agents.claude_code import (
    ClaudeCodeCLI,
    ClaudeCodeModelClient,
    claude_code_turn,
    clean_env,
    worker_args,
)
from superagent.agents.worker import WorkerSpec
from superagent.llm.client import ModelError, TransientModelError

FAKE = r'''#!PYTHON
"""A stand-in for the claude command line, driven by FAKECC_MODE."""
import json, os, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKECC_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(),
                          "name": os.environ.get("SUPERAGENT_NAME"), "nested": "CLAUDECODE" in os.environ}) + "\n")
mode = os.environ.get("FAKECC_MODE", "report")
events = None
if "--mcp-config" in args:
    config = json.loads(args[args.index("--mcp-config") + 1])
    events = config["mcpServers"]["superagent"]["env"]["SUPERAGENT_EVENTS_FILE"]
def emit(event):
    with open(events, "a") as handle:
        handle.write(json.dumps(event) + "\n")
if mode == "sleep":
    time.sleep(30)
if mode == "report":
    emit({"kind": "reply", "text": "Done.", "purpose": "result", "trigger_id": "t1"})
    emit({"kind": "report", "status": "progress", "summary": "half way", "question": "", "question_about": "",
          "pull_request": ""})
    emit({"kind": "hand_back", "trigger_id": "t2", "reason": "beyond scope"})
    emit({"kind": "report", "status": "ready", "summary": "all done", "question": "", "question_about": "",
          "pull_request": "https://github.com/o/r/pull/3"})
    print(json.dumps({"type": "result", "is_error": False, "result": "finished", "session_id": "sess-1"}))
elif mode == "noreport":
    print(json.dumps({"type": "result", "is_error": False, "result": "I stopped here.", "session_id": "sess-1"}))
elif mode == "overloaded":
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 529, "result": "Overloaded"}))
    sys.exit(1)
elif mode == "broken":
    print("not json at all")
    sys.exit(2)
elif mode == "structured":
    print(json.dumps({"type": "result", "is_error": False, "result": "{}", "structured_output": {"decision": "ignore"}}))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKECC_LOG", str(log))
    monkeypatch.setenv("CLAUDECODE", "1")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return ClaudeCodeCLI(str(path)), calls, monkeypatch


class Events:
    def __init__(self):
        self.read, self.progress_reports = [], []

    def inputs_read(self, ticket_id, ids):
        self.read.extend(ids)

    def progress(self, ticket_id, assignment_id, report):
        self.progress_reports.append(report)


class Host:
    def __init__(self):
        self.calls = []

    def reply(self, text, purpose, trigger_id):
        self.calls.append(("reply", text, purpose, trigger_id))
        return ""

    def hand_back(self, trigger_id, reason):
        self.calls.append(("hand_back", trigger_id, reason))
        return ""

    def follow(self, ref):
        self.calls.append(("follow", ref))
        return ""


@pytest.fixture
def spec(tmp_path):
    directory = tmp_path / "work" / "fix"
    directory.mkdir(parents=True)
    return WorkerSpec(ticket_id="tk_1", assignment_id="as_1", name="fix", directory=directory,
                      session_path=tmp_path / "sessions" / "tk_1.json", system="You are a worker.",
                      model="claude-opus-5-5", effort="high", env={"SUPERAGENT_NAME": "fix"},
                      blocked_commands=("sudo",), turn_timeout_seconds=20)


def test_turn_runs_claude_and_applies_bridge_events(fake, spec):
    cli, calls, _ = fake
    events, host = Events(), Host()
    report = claude_code_turn(spec, cli, [("in_1", "<input>fix it</input>")], events, host=host)
    assert report.status == "ready" and report.pull_request == "https://github.com/o/r/pull/3"
    assert events.read == ["in_1"] and [r.summary for r in events.progress_reports] == ["half way"]
    assert host.calls == [("reply", "Done.", "result", "t1"), ("hand_back", "t2", "beyond scope")]
    [call] = calls()
    assert call["prompt"] == "<input>fix it</input>" and call["name"] == "fix" and not call["nested"]
    assert Path(call["cwd"]).resolve() == spec.directory.resolve()
    args = call["args"]
    assert args[args.index("--model") + 1] == "claude-opus-5-5" and "--session-id" in args
    assert "You are a worker." in args[args.index("--append-system-prompt") + 1]


def test_later_turns_resume_the_session(fake, spec):
    cli, calls, _ = fake
    claude_code_turn(spec, cli, [("in_1", "first")], Events(), host=Host())
    claude_code_turn(spec, cli, [("in_2", "second")], Events(), host=Host())
    second = calls()[1]["args"]
    assert second[second.index("--resume") + 1] == "sess-1"


def test_turn_without_a_report_counts_as_waiting(fake, spec):
    cli, _, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "noreport")
    report = claude_code_turn(spec, cli, [("in_1", "go")], Events(), host=Host())
    assert report.status == "waiting" and report.summary == "I stopped here."


def test_passing_errors_are_retried_then_fail(fake, spec):
    cli, calls, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "overloaded")
    sleeps = []
    report = claude_code_turn(spec, cli, [("in_1", "go")], Events(), host=Host(), sleep=sleeps.append)
    assert report.status == "failed" and "Overloaded" in report.summary
    assert sleeps == [10.0, 60.0] and len(calls()) == 3


def test_unreadable_output_fails_the_turn(fake, spec):
    cli, _, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "broken")
    report = claude_code_turn(spec, cli, [("in_1", "go")], Events(), host=Host())
    assert report.status == "failed" and "not json" in report.summary


def test_cancel_stops_a_running_turn(fake, spec):
    cli, _, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "sleep")
    start = time.monotonic()
    report = claude_code_turn(spec, cli, [("in_1", "go")], Events(), host=Host(),
                              cancelled=lambda: time.monotonic() - start > 0.5)
    assert report is None and time.monotonic() - start < 15


def test_timeout_blocks_the_ticket(fake, spec):
    cli, _, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "sleep")
    spec.turn_timeout_seconds = 1
    report = claude_code_turn(spec, cli, [("in_1", "go")], Events(), host=Host())
    assert report.status == "blocked" and "1 seconds" in report.summary


def test_worker_args_allow_bridge_tools_and_deny_blocked_commands(spec, tmp_path):
    args = worker_args(spec, "sid", resume=False, events_path=tmp_path / "e.jsonl")
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "Bash" in allowed and "mcp__superagent__report" in allowed
    assert args[args.index("--disallowedTools") + 1] == "Bash(sudo *),Bash(sudo)"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    config = json.loads(args[args.index("--mcp-config") + 1])
    assert config["mcpServers"]["superagent"]["args"] == ["-m", "superagent.bridge"]


def test_model_client_returns_structured_output(fake):
    cli, calls, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "structured")
    response = ClaudeCodeModelClient(cli).create(model="claude-opus-5-5", system="judge", effort="low",
                                                 messages=[{"role": "user", "content": "card"}],
                                                 output_schema={"type": "object"})
    assert response.json() == {"decision": "ignore"}
    args = calls()[0]["args"]
    assert args[args.index("--tools") + 1] == "" and args[args.index("--system-prompt") + 1] == "judge"
    assert json.loads(args[args.index("--json-schema") + 1]) == {"type": "object"}
    assert calls()[0]["prompt"] == "card"


def test_model_client_classifies_errors(fake):
    cli, _, monkeypatch = fake
    monkeypatch.setenv("FAKECC_MODE", "overloaded")
    client = ClaudeCodeModelClient(cli)
    with pytest.raises(TransientModelError):
        client.create(model="m", system="s", messages=[{"role": "user", "content": "x"}])
    monkeypatch.setenv("FAKECC_MODE", "broken")
    with pytest.raises(ModelError):
        client.create(model="m", system="s", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(ModelError):
        client.create(model="m", system="s", messages=[], tools=[{"name": "bash"}])


def test_clean_env_drops_parent_session_variables(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    env = clean_env({"SUPERAGENT_NAME": "fix"})
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["SUPERAGENT_NAME"] == "fix" and env["PATH"] == os.environ["PATH"]
