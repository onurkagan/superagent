"""The Claude Code backend: workers and assessments run through the ``claude`` command line.

This backend needs no Anthropic API key. It uses whatever the ``claude`` command is logged in with, so check that your
plan allows unattended use. A worker turn is one ``claude -p`` process in the ticket directory that continues the
ticket's Claude Code session (``--session-id`` on the first turn, ``--resume`` afterwards). It works with Claude
Code's own shell and file tools; superagent's tools come from the MCP bridge in :mod:`superagent.bridge`, whose event
file this module reads while the turn runs. An assessment is one tool-less ``claude -p`` call with ``--json-schema``.

Differences from the API backend: Inputs that arrive while a turn runs are delivered at the next turn instead of the
next step, and the step limit is replaced by a turn timeout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from superagent.agents.tools import WorkerHost
from superagent.agents.worker import WorkerEvents, WorkerSpec
from superagent.bridge import SERVER_NAME, tool_names
from superagent.core.model import Report
from superagent.llm.client import ModelError, ModelResponse, TransientModelError

BUILTIN_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite")
TRANSIENT_STATUSES = (408, 409, 429, 500, 502, 503, 504, 529)
RETRY_DELAYS_SECONDS = (10.0, 60.0)
POLL_SECONDS = 0.5
_SESSION_NAMESPACE = uuid.UUID("6f1b9e1c-6a8a-4c5e-9a43-5e2a8e0f2c11")

BACKEND_NOTE = """# Tools in this environment

You run inside Claude Code. Use its own tools (Bash, Read, Edit, Write, Glob, Grep) for shell commands and files. The
superagent tools are named mcp__superagent__reply, mcp__superagent__report, mcp__superagent__hand_back and
mcp__superagent__follow_thread; wherever these instructions say `reply`, `report`, `hand_back` or `follow_thread`,
use those. Always end a turn by calling mcp__superagent__report."""


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The current environment without variables a parent Claude Code session sets, so a nested ``claude`` behaves
    like one started from a terminal."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith("CLAUDE") or k == "CLAUDECODE")}
    env.update(extra or {})
    return env


@dataclass
class CLIResult:
    exit_code: int
    data: dict[str, Any]
    stderr: str
    timed_out: bool = False
    cancelled: bool = False

    @property
    def is_error(self) -> bool:
        return self.exit_code != 0 or bool(self.data.get("is_error")) or self.timed_out

    @property
    def transient(self) -> bool:
        status = self.data.get("api_error_status")
        return isinstance(status, int) and status in TRANSIENT_STATUSES

    def error_text(self) -> str:
        if self.timed_out:
            return "the turn timed out"
        text = str(self.data.get("result") or "").strip() or self.stderr.strip() or f"exit code {self.exit_code}"
        return text[:1000]


class ClaudeCodeCLI:
    """Runs ``claude -p`` without a pipe that could fill up, with polling, cancelling and a timeout."""

    def __init__(self, command: str = "claude") -> None:
        self.command = command

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def run(
        self,
        args: list[str],
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        on_poll: Callable[[], None] = lambda: None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> CLIResult:
        with tempfile.TemporaryDirectory(prefix="superagent-claude-") as scratch:
            prompt_path = Path(scratch) / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            out_path, err_path = Path(scratch) / "out.json", Path(scratch) / "err.txt"
            with prompt_path.open("rb") as stdin, out_path.open("wb") as stdout, err_path.open("wb") as stderr:
                try:
                    process = subprocess.Popen([self.command, *args], cwd=cwd, env=env, stdin=stdin, stdout=stdout,
                                               stderr=stderr, start_new_session=True)
                except OSError as exc:
                    raise ModelError(f"cannot start {self.command}: {exc}") from exc
                deadline = time.monotonic() + timeout
                timed_out = was_cancelled = False
                while process.poll() is None:
                    on_poll()
                    if cancelled():
                        was_cancelled = True
                    elif time.monotonic() > deadline:
                        timed_out = True
                    if was_cancelled or timed_out:
                        process.terminate()
                        try:
                            process.wait(10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
                    try:
                        process.wait(POLL_SECONDS)
                    except subprocess.TimeoutExpired:
                        pass
                on_poll()
            raw = out_path.read_text(encoding="utf-8", errors="replace").strip()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"is_error": True, "result": raw[-1000:]}
            return CLIResult(process.returncode or 0, data if isinstance(data, dict) else {},
                             err_path.read_text(encoding="utf-8", errors="replace")[-2000:], timed_out, was_cancelled)


class ClaudeCodeModelClient:
    """A :class:`~superagent.llm.client.ModelClient` for single structured calls, such as the assessor's."""

    def __init__(self, cli: ClaudeCodeCLI | None = None, timeout: float = 600.0) -> None:
        self.cli = cli or ClaudeCodeCLI()
        self.timeout = timeout

    def create(self, *, model: str, system: str, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]] | None = None, effort: str = "high", max_tokens: int = 16000,
               output_schema: dict[str, Any] | None = None) -> ModelResponse:
        if tools:
            raise ModelError("the Claude Code model client runs tool-less calls only; workers use claude_code_turn")
        prompt = _text_of(messages[-1]["content"]) if messages else ""
        args = ["-p", "--output-format", "json", "--model", model, "--effort", effort, "--system-prompt", system,
                "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--permission-prompts", "none"]
        if output_schema is not None:
            args += ["--json-schema", json.dumps(output_schema)]
        with tempfile.TemporaryDirectory(prefix="superagent-assess-") as cwd:
            result = self.cli.run(args, prompt, Path(cwd), clean_env(), self.timeout)
        if result.is_error:
            error = TransientModelError if (result.transient or result.timed_out) else ModelError
            raise error(f"claude failed: {result.error_text()}")
        structured = result.data.get("structured_output")
        text = json.dumps(structured) if structured is not None else str(result.data.get("result") or "")
        return ModelResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn",
                             usage={"total_cost_usd": result.data.get("total_cost_usd")})


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _session_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _bridge_config(events_path: Path) -> str:
    package_root = str(Path(__file__).resolve().parents[2])
    pythonpath = os.pathsep.join(p for p in (package_root, os.environ.get("PYTHONPATH", "")) if p)
    return json.dumps({"mcpServers": {SERVER_NAME: {
        "type": "stdio", "command": sys.executable, "args": ["-m", "superagent.bridge"],
        "env": {"SUPERAGENT_EVENTS_FILE": str(events_path), "PYTHONPATH": pythonpath},
    }}})


def worker_args(spec: WorkerSpec, session_id: str, resume: bool, events_path: Path) -> list[str]:
    blocked = [f"Bash({name} *)" for name in spec.blocked_commands] + [f"Bash({name})" for name in spec.blocked_commands]
    args = [
        "-p", "--output-format", "json", "--model", spec.model, "--effort", spec.effort,
        "--append-system-prompt", f"{spec.system}\n\n{BACKEND_NOTE}",
        "--mcp-config", _bridge_config(events_path), "--strict-mcp-config",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--allowedTools", ",".join([*BUILTIN_TOOLS, *tool_names()]),
        "--setting-sources", "project,local",
    ]
    if blocked:
        args += ["--disallowedTools", ",".join(blocked)]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    return args


class _EventReader:
    """Reads the bridge's event file incrementally and dispatches each event."""

    def __init__(self, path: Path, spec: WorkerSpec, events: WorkerEvents, host: WorkerHost | None) -> None:
        self.path = path
        self.spec = spec
        self.events = events
        self.host = host
        self.offset = 0
        self.final: Report | None = None

    def __call__(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            data = handle.read()
        complete = data[: data.rfind(b"\n") + 1]
        self.offset += len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                self._dispatch(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "report":
            report = Report(status=event["status"], summary=event["summary"], question=event.get("question", ""),
                            question_trigger=event.get("question_about", ""),
                            pull_request=event.get("pull_request", ""))
            if report.status == "progress":
                self.events.progress(self.spec.ticket_id, self.spec.assignment_id, report)
            elif self.final is None:
                self.final = report
        elif self.host is None:
            return
        elif kind == "reply":
            self.host.reply(event["text"], event["purpose"], event.get("trigger_id", ""))
        elif kind == "hand_back":
            self.host.hand_back(event["trigger_id"], event.get("reason", ""))
        elif kind == "follow":
            self.host.follow(event["ref"])


def claude_code_turn(
    spec: WorkerSpec,
    client: ClaudeCodeCLI,
    inputs: list[tuple[str, str]],
    events: WorkerEvents,
    host: WorkerHost | None = None,
    inbox: Callable[[], list[tuple[str, str]]] = lambda: [],
    cancelled: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> Report | None:
    """Run one worker turn as a ``claude -p`` process. Same contract as :func:`superagent.agents.worker.run_turn`."""
    pending = list(inputs) + inbox()
    if cancelled():
        return None
    state = _session_state(spec.session_path)
    session_id = state.get("claude_session_id") or str(uuid.uuid5(_SESSION_NAMESPACE, spec.ticket_id))
    prompt = "\n\n".join(text for _, text in pending) or "Continue with the ticket, then report."
    events_path = spec.session_path.with_suffix(".events.jsonl")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")
    if pending:
        events.inputs_read(spec.ticket_id, [input_id for input_id, _ in pending])
    attempt = 0
    while True:
        reader = _EventReader(events_path, spec, events, host)
        args = worker_args(spec, session_id, bool(state.get("started")), events_path)
        result = client.run(args, prompt, spec.directory, clean_env(spec.env), spec.turn_timeout_seconds,
                            on_poll=reader, cancelled=cancelled)
        if result.data.get("session_id") or not result.is_error:
            state = {"claude_session_id": result.data.get("session_id") or session_id, "started": True}
            session_id = state["claude_session_id"]
            spec.session_path.parent.mkdir(parents=True, exist_ok=True)
            spec.session_path.write_text(json.dumps(state), encoding="utf-8")
        if result.cancelled or cancelled():
            return None
        if reader.final is not None:
            return reader.final
        if result.timed_out:
            return Report("blocked", f"The turn ran longer than {spec.turn_timeout_seconds} seconds and was stopped; "
                                     "a person should look at this ticket.")
        if result.is_error:
            if result.transient and attempt < len(RETRY_DELAYS_SECONDS):
                sleep(RETRY_DELAYS_SECONDS[attempt])
                attempt += 1
                prompt = "The previous attempt was interrupted by a passing error. Continue, then report."
                continue
            return Report("failed", f"Claude Code failed: {result.error_text()}")
        return Report("waiting", str(result.data.get("result") or "The turn ended without a report.")[:1000])
