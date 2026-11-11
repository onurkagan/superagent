import io
import json
import subprocess
import sys
from pathlib import Path

from superagent.bridge import EventLog, handle, serve, tool_names


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def call(log, name, **arguments):
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}, log)
    return response["result"]


def test_initialize_echoes_the_protocol_version_and_lists_four_tools(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, log)
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    tools = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, log)["result"]["tools"]
    assert [t["name"] for t in tools] == ["reply", "report", "hand_back", "follow_thread"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    assert tool_names()[0] == "mcp__superagent__reply"


def test_notifications_get_no_answer_and_unknown_methods_an_error(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, log) is None
    assert handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, log)["error"]["code"] == -32601


def test_tool_calls_are_validated_and_recorded(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path))
    assert not call(log, "reply", text="On it.", purpose="acknowledgement", in_reply_to="t1")["isError"]
    assert call(log, "reply", text="word " * 200, purpose="result", in_reply_to="")["isError"]
    assert call(log, "reply", text="hi", purpose="gossip", in_reply_to="")["isError"]
    result = call(log, "report", status="ready", summary="done", question="", question_about="",
                  pull_request="https://github.com/o/r/pull/1")
    assert "turn is over" in result["content"][0]["text"]
    assert call(log, "report", status="bogus", summary="x", question="", question_about="", pull_request="")["isError"]
    call(log, "hand_back", message_id="t2", reason="beyond scope")
    call(log, "follow_thread", ref="o/r#4")
    assert [e["kind"] for e in events(path)] == ["reply", "report", "hand_back", "follow"]
    assert events(path)[1]["pull_request"] == "https://github.com/o/r/pull/1"


def test_serve_speaks_newline_delimited_json_rpc(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n\n'
                        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    stdout = io.StringIO()
    serve(stdin, stdout, log)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert lines[1]["error"]["code"] == -32700 and len(lines) == 2


def test_module_runs_as_a_subprocess(tmp_path):
    path = tmp_path / "events.jsonl"
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "hand_back", "arguments": {"message_id": "m", "reason": "r"}}}
    process = subprocess.run([sys.executable, "-m", "superagent.bridge"], input=json.dumps(request) + "\n",
                             capture_output=True, text=True, timeout=30,
                             env={"SUPERAGENT_EVENTS_FILE": str(path), "PYTHONPATH": str(Path.cwd())})
    assert json.loads(process.stdout)["id"] == 7
    assert events(path) == [{"kind": "hand_back", "trigger_id": "m", "reason": "r"}]


def test_orchestrator_role_lists_and_runs_orchestrator_tools(tmp_path):
    from superagent.core.model import Mandate, Ticket
    from superagent.store import Store

    database = tmp_path / "superagent.db"
    store = Store(database)
    store.put(Ticket(id="tk_1", name="add-cli", title="Add a CLI", decision_id="dc_1", origin_thread="inbox:cli",
                     origin_trigger="inbox:0", mandate=Mandate("p-1", "explicit", "allowlist", "Add a CLI", "me",
                                                               "inbox")))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_tickets", "arguments": {"which": "open"}}},
    ]
    process = subprocess.run([sys.executable, "-m", "superagent.bridge"],
                             input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True,
                             timeout=30, env={"SUPERAGENT_ROLE": "orchestrator", "SUPERAGENT_DB": str(database),
                                              "PYTHONPATH": str(Path.cwd())})
    listed, called = [json.loads(line) for line in process.stdout.splitlines()]
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "route" in names and "note" in names and "reply" not in names
    assert "tk_1 [add-cli]" in called["result"]["content"][0]["text"]


def test_orchestrator_role_needs_a_database():
    process = subprocess.run([sys.executable, "-m", "superagent.bridge"], input="", capture_output=True, text=True,
                             timeout=30, env={"SUPERAGENT_ROLE": "orchestrator", "PYTHONPATH": str(Path.cwd())})
    assert process.returncode == 2 and "SUPERAGENT_DB" in process.stderr
