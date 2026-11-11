"""The worker's tools: their definitions for the model and their implementations.

File tools stay inside the ticket directory and the shell refuses blocked commands by name. These are defaults that
prevent mistakes, not a security boundary: a command can still reach any path the operator's user can. Tools that
reach people or the domain (``reply``, ``report``, ``hand_back``, ``follow_thread``) go through a :class:`WorkerHost`,
and the Engine checks them again.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from superagent.core.model import REPORT_STATUSES, Report

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 1800
MAX_OUTPUT_CHARS = 20000
DEFAULT_READ_LINES = 2000
MAX_READ_CHARS = 100000


class WorkerHost(Protocol):
    def reply(self, text: str, purpose: str, trigger_id: str) -> str:
        """Queue a message; returns an error text or empty on success."""

    def hand_back(self, trigger_id: str, reason: str) -> str: ...

    def follow(self, ref: str) -> str:
        """Follow another conversation for context; returns its Thread key, or empty when unknown."""


@dataclass
class ToolContext:
    directory: Path
    env: dict[str, str] = field(default_factory=dict)
    blocked_commands: tuple[str, ...] = ()
    host: WorkerHost | None = None
    cancelled: Callable[[], bool] = lambda: False


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    report: Report | None = None


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True, "input_schema": _schema(properties)}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool("bash",
          "Run a shell command in the ticket directory and return its exit code and combined output. Use it for "
          "git, gh, builds, tests and searching (rg, grep, find). Long output is cut in the middle. Commands time "
          f"out after timeout_seconds (0 means {DEFAULT_TIMEOUT_SECONDS}, at most {MAX_TIMEOUT_SECONDS}).",
          {"command": STRING, "timeout_seconds": INTEGER}),
    _tool("read_file",
          "Read a text file inside the ticket directory, with line numbers. offset is the first line to read "
          f"(1-based, 0 for the start); limit is the number of lines (0 means {DEFAULT_READ_LINES}).",
          {"path": STRING, "offset": INTEGER, "limit": INTEGER}),
    _tool("write_file",
          "Create or overwrite a file inside the ticket directory with the given content. Parent directories are "
          "created. Prefer edit_file for changes to existing files.",
          {"path": STRING, "content": STRING}),
    _tool("edit_file",
          "Replace old_text with new_text in a file inside the ticket directory. old_text must match exactly and be "
          "unique unless replace_all is true.",
          {"path": STRING, "old_text": STRING, "new_text": STRING, "replace_all": {"type": "boolean"}}),
    _tool("reply",
          "Send a short message to a person, in the conversation of the message with id in_reply_to (empty for the "
          "ticket's original conversation). purpose is acknowledgement (confirming a new request once), answer "
          "(answering a person's question) or result (delivering something ready to review or use). Hard limit: "
          "150 words.",
          {"text": STRING, "purpose": {"type": "string", "enum": ["acknowledgement", "answer", "result"]},
           "in_reply_to": STRING}),
    _tool("report",
          "Report the ticket's state to the host. Every status except progress ends your turn. Put one concrete "
          "question in question when you need a person (set question_about to the id of the message that prompted "
          "it, or empty); otherwise leave it empty. Set pull_request to the URL of a pull request you opened.",
          {"status": {"type": "string", "enum": list(REPORT_STATUSES)}, "summary": STRING, "question": STRING,
           "question_about": STRING, "pull_request": STRING}),
    _tool("hand_back",
          "Return a person's message that asks for work beyond this ticket's scope, belongs to another ticket, or "
          "asks to stop. message_id is the id of the <input> card's trigger. Do not answer the message yourself.",
          {"message_id": STRING, "reason": STRING}),
    _tool("follow_thread",
          "Follow another conversation, such as a related issue or pull request (a URL or #number), so its new "
          "messages reach this ticket as context. Following grants no authority.",
          {"ref": STRING}),
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_DEFINITIONS)

_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&\n]|\$\(|`|\(|\)")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "xargs", "sudo", "nice"})


def blocked_command(command: str, blocked: tuple[str, ...] | list[str]) -> str:
    """Return the first blocked program a command invokes by name, or empty."""
    if not blocked:
        return ""
    names = {b.strip() for b in blocked if b.strip()}
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        for word in words:
            if _ASSIGNMENT.match(word):
                continue
            program = os.path.basename(word)
            if program in names:
                return program
            if program in _WRAPPERS or word.startswith("-"):
                continue
            break
    return ""


def _clip_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n\n... [{len(text) - MAX_OUTPUT_CHARS} characters omitted] ...\n\n{text[-half:]}"


def _shell() -> list[str]:
    bash = shutil.which("bash")
    return [bash, "-c"] if bash else ["/bin/sh", "-c"]


def run_bash(ctx: ToolContext, command: str, timeout_seconds: int = 0) -> ToolResult:
    if not command.strip():
        return ToolResult("the command is empty", is_error=True)
    program = blocked_command(command, ctx.blocked_commands)
    if program:
        return ToolResult(f"refused: `{program}` is a blocked command in this deployment", is_error=True)
    timeout = timeout_seconds if timeout_seconds > 0 else DEFAULT_TIMEOUT_SECONDS
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)
    env = {**os.environ, **ctx.env}
    try:
        process = subprocess.Popen([*_shell(), command], cwd=ctx.directory, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return ToolResult(f"could not start the shell: {exc}", is_error=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        output, _ = process.communicate()
        text = output.decode("utf-8", "replace")
        return ToolResult(f"timed out after {timeout}s and was killed\n{_clip_output(text)}", is_error=True)
    text = output.decode("utf-8", "replace")
    return ToolResult(f"exit code {process.returncode}\n{_clip_output(text)}", is_error=process.returncode != 0)


def _resolve(ctx: ToolContext, path: str) -> Path:
    root = ctx.directory.resolve()
    candidate = Path(path)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{path} is outside the ticket directory; use bash for other paths")
    return target


def read_file(ctx: ToolContext, path: str, offset: int = 0, limit: int = 0) -> ToolResult:
    try:
        target = _resolve(ctx, path)
        lines = target.read_text(encoding="utf-8").splitlines()
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)
    except FileNotFoundError:
        return ToolResult(f"{path} does not exist", is_error=True)
    except IsADirectoryError:
        return ToolResult(f"{path} is a directory; use bash with ls or find", is_error=True)
    except UnicodeDecodeError:
        return ToolResult(f"{path} is not a UTF-8 text file", is_error=True)
    start = max(offset, 1)
    count = limit if limit > 0 else DEFAULT_READ_LINES
    chosen = lines[start - 1:start - 1 + count]
    width = len(str(start + len(chosen)))
    body = "\n".join(f"{n:>{width}}\t{line}" for n, line in enumerate(chosen, start))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n... [cut; read a smaller range]"
    remaining = len(lines) - (start - 1 + len(chosen))
    if remaining > 0:
        body += f"\n... [{remaining} more lines; continue with offset {start + len(chosen)}]"
    return ToolResult(body if lines else "(empty file)")


def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    try:
        target = _resolve(ctx, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except (ValueError, OSError) as exc:
        return ToolResult(str(exc), is_error=True)
    return ToolResult(f"wrote {len(content.encode('utf-8'))} bytes to {path}")


def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
    try:
        target = _resolve(ctx, path)
        original = target.read_text(encoding="utf-8")
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError) as exc:
        return ToolResult(f"cannot edit {path}: {exc}", is_error=True)
    if not old_text:
        return ToolResult("old_text is empty; use write_file to create a file", is_error=True)
    count = original.count(old_text)
    if count == 0:
        return ToolResult(f"old_text was not found in {path}; read the file and copy the text exactly", is_error=True)
    if count > 1 and not replace_all:
        return ToolResult(f"old_text appears {count} times in {path}; add surrounding context or set replace_all",
                          is_error=True)
    updated = original.replace(old_text, new_text) if replace_all else original.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8")
    return ToolResult(f"replaced {count if replace_all else 1} occurrence(s) in {path}")


def execute(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Run one tool call. Unknown tools and bad arguments come back as error results the model can correct."""
    if not isinstance(arguments, dict):
        return ToolResult("the tool input must be a JSON object", is_error=True)
    try:
        if name == "bash":
            return run_bash(ctx, str(arguments["command"]), int(arguments.get("timeout_seconds") or 0))
        if name == "read_file":
            return read_file(ctx, str(arguments["path"]), int(arguments.get("offset") or 0),
                             int(arguments.get("limit") or 0))
        if name == "write_file":
            return write_file(ctx, str(arguments["path"]), str(arguments["content"]))
        if name == "edit_file":
            return edit_file(ctx, str(arguments["path"]), str(arguments["old_text"]), str(arguments["new_text"]),
                             bool(arguments.get("replace_all")))
        if name == "report":
            return _report(arguments)
        if ctx.host is None:
            return ToolResult(f"{name} is not available here", is_error=True)
        if name == "reply":
            error = ctx.host.reply(str(arguments["text"]), str(arguments["purpose"]),
                                   str(arguments.get("in_reply_to") or ""))
            return ToolResult(error, is_error=True) if error else ToolResult("queued for delivery")
        if name == "hand_back":
            error = ctx.host.hand_back(str(arguments["message_id"]), str(arguments.get("reason") or ""))
            return ToolResult(error, is_error=True) if error else ToolResult("handed back; do not answer it")
        if name == "follow_thread":
            key = ctx.host.follow(str(arguments["ref"]))
            return ToolResult(f"following {key}") if key else ToolResult("unknown conversation", is_error=True)
    except KeyError as exc:
        return ToolResult(f"missing argument {exc.args[0]!r}", is_error=True)
    except (TypeError, ValueError) as exc:
        return ToolResult(f"invalid arguments: {exc}", is_error=True)
    return ToolResult(f"unknown tool {name!r}", is_error=True)


def _report(arguments: dict[str, Any]) -> ToolResult:
    status = str(arguments["status"])
    if status not in REPORT_STATUSES:
        return ToolResult(f"unknown status {status!r}", is_error=True)
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        return ToolResult("the summary is empty", is_error=True)
    question = str(arguments.get("question") or "").strip()
    report = Report(status=status, summary=summary, question=question,  # type: ignore[arg-type]
                    question_trigger=str(arguments.get("question_about") or ""),
                    pull_request=str(arguments.get("pull_request") or "").strip())
    return ToolResult("recorded" if status == "progress" else "recorded; your turn ends here", report=report)
