import json

import pytest

from superagent.agents.worker import CUT_OFF_NOTE, Session, WorkerSpec, run_turn
from superagent.llm.client import ModelError, ModelResponse, TransientModelError
from superagent.llm.fake import ScriptedModelClient, text, tool_call, tool_calls

REPORT_READY = dict(status="ready", summary="done", question="", question_about="", pull_request="")


class Events:
    def __init__(self):
        self.read = []
        self.progress_reports = []

    def inputs_read(self, ticket_id, input_ids):
        self.read.extend(input_ids)

    def progress(self, ticket_id, assignment_id, report):
        self.progress_reports.append(report)


@pytest.fixture
def spec(tmp_path):
    return WorkerSpec(ticket_id="tk_1", assignment_id="as_1", name="fix", directory=tmp_path,
                      session_path=tmp_path / ".session.json", system="You are a worker.", model="claude-opus-5-5",
                      max_steps=10)


def test_turn_runs_tools_until_a_terminal_report(spec):
    client = ScriptedModelClient([
        tool_call("write_file", path="hello.txt", content="hi"),
        tool_calls(("report", {**REPORT_READY, "status": "progress", "summary": "wrote file"}),
                   ("bash", {"command": "cat hello.txt", "timeout_seconds": 0})),
        tool_call("report", **REPORT_READY),
    ])
    events = Events()
    report = run_turn(spec, client, [("in_1", "<input>fix it</input>")], events)
    assert report.status == "ready"
    assert events.read == ["in_1"]
    assert [r.summary for r in events.progress_reports] == ["wrote file"]
    assert (spec.directory / "hello.txt").read_text() == "hi"
    request = client.requests[2]
    assert request["tools"][0]["name"] == "bash"
    assert request["messages"][-1]["content"][1]["content"] == "exit code 0\nhi"


def test_session_persists_and_new_inputs_merge_into_tool_results(spec):
    client = ScriptedModelClient([tool_call("report", **{**REPORT_READY, "status": "waiting"})])
    run_turn(spec, client, [("in_1", "first")], Events())
    stored = Session(spec.session_path).messages
    assert [m["role"] for m in stored] == ["user", "assistant", "user"]
    client.add(tool_call("report", **REPORT_READY))
    run_turn(spec, client, [("in_2", "second")], Events())
    second_request = client.requests[1]["messages"]
    assert second_request[2]["content"][0]["type"] == "tool_result"
    assert second_request[2]["content"][1] == {"type": "text", "text": "second"}


def test_inbox_messages_are_taken_in_at_the_next_step(spec):
    deliveries = [[], [("in_2", "please also update the changelog")]]
    client = ScriptedModelClient([tool_call("bash", command="true", timeout_seconds=0),
                                  tool_call("report", **REPORT_READY)])
    events = Events()
    run_turn(spec, client, [("in_1", "start")], events, inbox=lambda: deliveries.pop(0) if deliveries else [])
    assert events.read == ["in_1", "in_2"]
    assert "changelog" in json.dumps(client.requests[1]["messages"][-1])


def test_ending_without_a_report_counts_as_waiting(spec):
    report = run_turn(spec, ScriptedModelClient([text("All done, I think.")]), [("in_1", "go")], Events())
    assert report.status == "waiting" and "All done" in report.summary


def test_transient_errors_are_retried_then_fail(spec):
    sleeps = []
    client = ScriptedModelClient([TransientModelError("429")] * 4)
    report = run_turn(spec, client, [("in_1", "go")], Events(), sleep=sleeps.append)
    assert report.status == "failed" and sleeps == [5.0, 20.0, 60.0]


def test_transient_error_then_success(spec):
    client = ScriptedModelClient([TransientModelError("overloaded"), tool_call("report", **REPORT_READY)])
    assert run_turn(spec, client, [("in_1", "go")], Events(), sleep=lambda s: None).status == "ready"


def test_permanent_error_and_refusal_fail_the_turn(spec):
    assert run_turn(spec, ScriptedModelClient([ModelError("401")]), [("i", "go")], Events()).status == "failed"
    refusal = ModelResponse(content=[], stop_reason="refusal", refusal_category="cyber")
    report = run_turn(spec, ScriptedModelClient([refusal]), [("i2", "go")], Events())
    assert report.status == "failed" and "cyber" in report.summary


def test_cut_off_tool_calls_are_not_executed(spec):
    cut = ModelResponse(content=[{"type": "tool_use", "id": "toolu_x", "name": "write_file",
                                  "input": {"path": "a.txt", "content": "partial"}}], stop_reason="max_tokens")
    client = ScriptedModelClient([cut, tool_call("report", **REPORT_READY)])
    run_turn(spec, client, [("in_1", "go")], Events())
    assert not (spec.directory / "a.txt").exists()
    assert "cut off" in json.dumps(client.requests[1]["messages"][-1])


def test_cut_off_text_asks_the_model_to_continue(spec):
    cut = ModelResponse(content=[{"type": "text", "text": "Long expla"}], stop_reason="max_tokens")
    client = ScriptedModelClient([cut, tool_call("report", **REPORT_READY)])
    run_turn(spec, client, [("in_1", "go")], Events())
    assert CUT_OFF_NOTE in json.dumps(client.requests[1]["messages"][-1])


def test_step_limit_blocks_the_ticket(spec):
    spec.max_steps = 2
    client = ScriptedModelClient([tool_call("bash", command="true", timeout_seconds=0)] * 2)
    report = run_turn(spec, client, [("in_1", "go")], Events())
    assert report.status == "blocked" and "2 model steps" in report.summary


def test_cancelled_turn_returns_none(spec):
    assert run_turn(spec, ScriptedModelClient([]), [("in_1", "go")], Events(), cancelled=lambda: True) is None
