from pathlib import Path

import pytest

from superagent.agents.tools import TOOL_DEFINITIONS, ToolContext, blocked_command, execute


class Host:
    def __init__(self):
        self.calls = []

    def reply(self, text, purpose, trigger_id):
        self.calls.append(("reply", text, purpose, trigger_id))
        return "" if len(text) < 50 else "too long"

    def hand_back(self, trigger_id, reason):
        self.calls.append(("hand_back", trigger_id, reason))
        return ""

    def follow(self, ref):
        return "github:o/r#5" if ref == "#5" else ""


@pytest.fixture
def ctx(tmp_path: Path):
    return ToolContext(directory=tmp_path, env={"SUPERAGENT_NAME": "fix-typo"}, blocked_commands=("rm", "shutdown"),
                       host=Host())


def test_every_definition_is_strict_with_all_fields_required():
    for tool in TOOL_DEFINITIONS:
        schema = tool["input_schema"]
        assert tool["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_bash_runs_in_the_ticket_directory_with_variables(ctx):
    result = execute("bash", {"command": "pwd; echo $SUPERAGENT_NAME", "timeout_seconds": 0}, ctx)
    assert not result.is_error
    assert str(ctx.directory.resolve()) in result.content and "fix-typo" in result.content


def test_bash_reports_failures_and_timeouts(ctx):
    assert execute("bash", {"command": "exit 3", "timeout_seconds": 0}, ctx).content.startswith("exit code 3")
    result = execute("bash", {"command": "sleep 5", "timeout_seconds": 1}, ctx)
    assert result.is_error and "timed out" in result.content


@pytest.mark.parametrize("command,blocked", [
    ("rm -rf build", "rm"),
    ("ls && rm x", "rm"),
    ("FOO=1 env rm x", "rm"),
    ("echo $(shutdown now)", "shutdown"),
    ("/bin/rm x", "rm"),
    ("echo rm", ""),
    ("git status", ""),
])
def test_blocked_commands_are_found_by_program_name(command, blocked):
    assert blocked_command(command, ("rm", "shutdown")) == blocked


def test_blocked_command_is_refused(ctx):
    result = execute("bash", {"command": "rm -rf .", "timeout_seconds": 0}, ctx)
    assert result.is_error and "blocked" in result.content


def test_write_read_and_edit_files(ctx):
    assert not execute("write_file", {"path": "docs/a.md", "content": "one\ntwo\ntwo\n"}, ctx).is_error
    read = execute("read_file", {"path": "docs/a.md", "offset": 0, "limit": 0}, ctx)
    assert "1\tone" in read.content and "3\ttwo" in read.content
    ambiguous = execute("edit_file", {"path": "docs/a.md", "old_text": "two", "new_text": "2",
                                      "replace_all": False}, ctx)
    assert ambiguous.is_error and "2 times" in ambiguous.content
    assert not execute("edit_file", {"path": "docs/a.md", "old_text": "two", "new_text": "2", "replace_all": True},
                       ctx).is_error
    assert (ctx.directory / "docs/a.md").read_text() == "one\n2\n2\n"


def test_read_file_paginates(ctx):
    (ctx.directory / "long.txt").write_text("\n".join(str(i) for i in range(1, 11)))
    result = execute("read_file", {"path": "long.txt", "offset": 4, "limit": 2}, ctx)
    assert result.content.startswith("4\t4\n5\t5") and "continue with offset 6" in result.content


def test_file_tools_stay_inside_the_ticket_directory(ctx):
    result = execute("read_file", {"path": "../outside.txt", "offset": 0, "limit": 0}, ctx)
    assert result.is_error and "outside" in result.content
    assert execute("write_file", {"path": "/etc/x", "content": ""}, ctx).is_error


def test_report_tool_builds_a_report(ctx):
    result = execute("report", {"status": "blocked", "summary": "need token", "question": "Can you add a token?",
                                "question_about": "", "pull_request": ""}, ctx)
    assert result.report.status == "blocked" and result.report.question == "Can you add a token?"
    assert execute("report", {"status": "bogus", "summary": "x", "question": "", "question_about": "",
                              "pull_request": ""}, ctx).is_error


def test_host_tools_pass_through_and_surface_errors(ctx):
    assert execute("reply", {"text": "On it.", "purpose": "acknowledgement", "in_reply_to": ""}, ctx).content == \
        "queued for delivery"
    assert execute("reply", {"text": "x" * 60, "purpose": "result", "in_reply_to": ""}, ctx).is_error
    assert not execute("hand_back", {"message_id": "t1", "reason": "beyond scope"}, ctx).is_error
    assert execute("follow_thread", {"ref": "#5"}, ctx).content == "following github:o/r#5"
    assert execute("follow_thread", {"ref": "nope"}, ctx).is_error


def test_unknown_tool_and_missing_arguments_are_errors(ctx):
    assert execute("teleport", {}, ctx).is_error
    assert "missing argument" in execute("bash", {}, ctx).content
