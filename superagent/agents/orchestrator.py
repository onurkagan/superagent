"""One orchestrator turn: the persistent session that oversees every ticket takes an update and acts on it.

The orchestrator is one long-lived conversation. Each turn adds an update (operator messages, candidates to route,
events since the last turn) and lets the model act with the tools in
:mod:`superagent.agents.orchestrator_tools` until it answers with plain text.

- ``anthropic`` backend: :func:`api_turn` runs the tool loop in this process and keeps the conversation in a session
  file. When the history grows past a limit it starts again with a note; the store, not the history, holds the truth.
- ``claude-code`` backend: :func:`claude_code_turn` runs one ``claude -p`` process that resumes the orchestrator's
  Claude Code session, with the tools served by the MCP bridge in its orchestrator role. Claude Code compacts the
  history itself.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from superagent.agents.claude_code import RETRY_DELAYS_SECONDS as CLI_RETRY_DELAYS
from superagent.agents.claude_code import ClaudeCodeCLI, _session_state, clean_env
from superagent.agents.orchestrator_tools import ORCHESTRATOR_TOOLS, TOOL_NAMES, OrchestratorTools
from superagent.agents.worker import CUT_OFF_NOTE, RETRY_DELAYS_SECONDS, Session
from superagent.bridge import SERVER_NAME, tool_names
from superagent.llm.client import ModelClient, ModelError, TransientModelError

RESET_NOTE = (
    "Your earlier conversation was cleared to keep it short. The tickets and conversations are unchanged: use "
    "list_tickets and read_ticket to see where things stand."
)
INTERRUPTED = "This call did not run: the host stopped before it finished. Check the state before repeating it."

CLAUDE_CODE_NOTE = """# Tools in this environment

You run inside Claude Code without shell or file tools. The orchestrator's tools are named
mcp__superagent__<tool>, such as mcp__superagent__list_tickets and mcp__superagent__route; wherever these
instructions name a tool, use that one."""


@dataclass
class OrchestratorSpec:
    """Everything an orchestrator turn needs, built by the runtime."""

    system: str
    model: str
    effort: str
    session_path: Path
    directory: Path
    db_path: str
    timeout_seconds: int = 900
    max_steps: int = 40
    max_tokens: int = 16000
    max_history_chars: int = 400_000


@dataclass
class TurnResult:
    text: str
    error: str = ""


def _size(session: Session) -> int:
    return len(json.dumps(session.messages, ensure_ascii=False))


def _repair(session: Session) -> None:
    """A turn cut off between a tool call and its result leaves the call unanswered; answer it."""
    if not session.messages or session.messages[-1]["role"] != "assistant":
        return
    uses = [b for b in session.messages[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if uses:
        session.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": u["id"], "content": INTERRUPTED, "is_error": True} for u in uses]})


def api_turn(spec: OrchestratorSpec, client: ModelClient, update: str, tools: OrchestratorTools,
             sleep: Callable[[float], None] = time.sleep) -> TurnResult:
    """Run one turn with the Messages API. Returns the model's closing text, or an error."""
    session = Session(spec.session_path)
    _repair(session)
    if session.messages and _size(session) > spec.max_history_chars:
        session.messages = []
        update = f"{RESET_NOTE}\n\n{update}"
    session.add_user_text([update])
    session.save()
    failures = 0
    for _ in range(spec.max_steps):
        try:
            response = client.create(model=spec.model, system=spec.system, messages=session.messages,
                                     tools=ORCHESTRATOR_TOOLS, effort=spec.effort, max_tokens=spec.max_tokens)
        except TransientModelError as exc:
            if failures >= len(RETRY_DELAYS_SECONDS):
                return TurnResult("", f"the model API kept failing: {exc}")
            sleep(RETRY_DELAYS_SECONDS[failures])
            failures += 1
            continue
        except ModelError as exc:
            return TurnResult("", f"the model call failed: {exc}")
        failures = 0
        if response.stop_reason == "refusal":
            return TurnResult("", f"the model declined the update ({response.refusal_category or 'no category'})")
        session.messages.append({"role": "assistant", "content": response.content})
        session.save()
        if response.stop_reason == "pause_turn":
            continue
        uses = response.tool_uses()
        if not uses:
            if response.stop_reason == "max_tokens":
                session.add_user_text([CUT_OFF_NOTE])
                session.save()
                continue
            return TurnResult(response.text())
        results = []
        for use in uses:
            if response.stop_reason == "max_tokens":
                content, is_error = "This call was cut off at the output limit; make it again.", True
            else:
                content, is_error = tools.execute(use["name"], use.get("input") or {})
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use["id"], "content": content}
            if is_error:
                block["is_error"] = True
            results.append(block)
        session.messages.append({"role": "user", "content": results})
        session.save()
    return TurnResult("", f"the turn stopped after {spec.max_steps} model steps")


def _bridge_config(db_path: str) -> str:
    package_root = str(Path(__file__).resolve().parents[2])
    pythonpath = os.pathsep.join(p for p in (package_root, os.environ.get("PYTHONPATH", "")) if p)
    return json.dumps({"mcpServers": {SERVER_NAME: {
        "type": "stdio", "command": sys.executable, "args": ["-m", "superagent.bridge"],
        "env": {"SUPERAGENT_ROLE": "orchestrator", "SUPERAGENT_DB": db_path, "PYTHONPATH": pythonpath},
    }}})


def orchestrator_args(spec: OrchestratorSpec, session_id: str, resume: bool) -> list[str]:
    args = [
        "-p", "--output-format", "json", "--model", spec.model, "--effort", spec.effort,
        "--system-prompt", f"{spec.system}\n\n{CLAUDE_CODE_NOTE}",
        "--mcp-config", _bridge_config(spec.db_path), "--strict-mcp-config", "--tools", "",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--allowedTools", ",".join(tool_names(TOOL_NAMES)),
    ]
    return args + (["--resume", session_id] if resume else ["--session-id", session_id])


def claude_code_turn(spec: OrchestratorSpec, cli: ClaudeCodeCLI, update: str,
                     sleep: Callable[[float], None] = time.sleep) -> TurnResult:
    """Run one turn as a ``claude -p`` process that resumes the orchestrator's Claude Code session."""
    state = _session_state(spec.session_path)
    session_id = state.get("claude_session_id") or str(uuid.uuid4())
    spec.directory.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        args = orchestrator_args(spec, session_id, resume=bool(state.get("started")))
        result = cli.run(args, update, spec.directory, clean_env(), spec.timeout_seconds)
        if result.data.get("session_id") or not result.is_error:
            state = {"claude_session_id": result.data.get("session_id") or session_id, "started": True}
            session_id = state["claude_session_id"]
            spec.session_path.parent.mkdir(parents=True, exist_ok=True)
            spec.session_path.write_text(json.dumps(state), encoding="utf-8")
        if not result.is_error:
            return TurnResult(str(result.data.get("result") or "").strip())
        if result.transient and attempt < len(CLI_RETRY_DELAYS):
            sleep(CLI_RETRY_DELAYS[attempt])
            attempt += 1
            continue
        if state.get("started") and "No conversation found" in result.error_text() and attempt == 0:
            state, session_id, attempt = {}, str(uuid.uuid4()), attempt + 1
            update = f"{RESET_NOTE}\n\n{update}"
            continue
        return TurnResult("", f"Claude Code failed: {result.error_text()}")
