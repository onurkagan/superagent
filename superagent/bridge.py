"""An MCP server that gives a Claude Code worker superagent's own tools.

With the Claude Code backend, a worker turn is a ``claude -p`` process that uses Claude Code's built-in shell and file
tools. The four tools that reach people and the domain (``reply``, ``report``, ``hand_back`` and ``follow_thread``)
come from this server, which Claude Code starts over stdio. The server validates each call and appends it as one JSON
line to the file named by ``SUPERAGENT_EVENTS_FILE``; the Executor reads that file while the turn runs and hands the
events to the Engine, which checks them again.

Run with ``python -m superagent.bridge``. It speaks the subset of the Model Context Protocol a tool server needs:
``initialize``, ``tools/list``, ``tools/call`` and ``ping``, as newline-delimited JSON-RPC 2.0.
"""

from __future__ import annotations

import json
import os
import sys
from typing import IO, Any

from superagent import __version__
from superagent.agents.tools import TOOL_DEFINITIONS, execute
from superagent.core.engine import MAX_REPLY_WORDS, REPLY_PURPOSES

SERVER_NAME = "superagent"
BRIDGED = ("reply", "report", "hand_back", "follow_thread")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


def tool_names() -> list[str]:
    """The names Claude Code gives the bridged tools."""
    return [f"mcp__{SERVER_NAME}__{name}" for name in BRIDGED]


def _tools() -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
            for t in TOOL_DEFINITIONS if t["name"] in BRIDGED]


class EventLog:
    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, event: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()


def call_tool(name: str, arguments: dict[str, Any], log: EventLog) -> tuple[str, bool]:
    """Validate a call, record it, and return the text the model sees and whether it is an error."""
    if not isinstance(arguments, dict):
        return "the tool input must be a JSON object", True
    if name == "reply":
        text = str(arguments.get("text") or "")
        purpose = str(arguments.get("purpose") or "")
        if purpose not in REPLY_PURPOSES:
            return f"unknown purpose {purpose!r}; use one of {', '.join(REPLY_PURPOSES)}", True
        words = len(text.split())
        if words == 0:
            return "the message is empty", True
        if words > MAX_REPLY_WORDS:
            return f"the message has {words} words; the limit is {MAX_REPLY_WORDS}. Keep details in the pull request.", True
        log.append({"kind": "reply", "text": text, "purpose": purpose,
                    "trigger_id": str(arguments.get("in_reply_to") or "")})
        return "queued for delivery", False
    if name == "report":
        result = execute("report", arguments, _NO_CONTEXT)
        if result.report is None:
            return result.content, True
        report = result.report
        log.append({"kind": "report", "status": report.status, "summary": report.summary,
                    "question": report.question, "question_about": report.question_trigger,
                    "pull_request": report.pull_request})
        if report.status == "progress":
            return "recorded", False
        return "recorded. Your turn is over: stop now and answer with one short line.", False
    if name == "hand_back":
        message_id = str(arguments.get("message_id") or "")
        if not message_id:
            return "message_id is required", True
        log.append({"kind": "hand_back", "trigger_id": message_id, "reason": str(arguments.get("reason") or "")})
        return "handed back; do not answer it", False
    if name == "follow_thread":
        ref = str(arguments.get("ref") or "").strip()
        if not ref:
            return "ref is required", True
        log.append({"kind": "follow", "ref": ref})
        return f"requested to follow {ref}; its new messages will reach this ticket if the reference is known", False
    return f"unknown tool {name!r}", True


class _NoContext:
    """``execute('report', ...)`` needs no directory or host."""

    directory = None
    env: dict[str, str] = {}
    blocked_commands: tuple[str, ...] = ()
    host = None


_NO_CONTEXT: Any = _NoContext()


def handle(message: dict[str, Any], log: EventLog) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; notifications get no answer."""
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        result: dict[str, Any] = {"protocolVersion": version, "capabilities": {"tools": {}},
                                  "serverInfo": {"name": SERVER_NAME, "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools()}
    elif method == "tools/call":
        params = message.get("params") or {}
        text, is_error = call_tool(str(params.get("name", "")), params.get("arguments") or {}, log)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(stdin: IO[str], stdout: IO[str], log: EventLog) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message, log) if isinstance(message, dict) else None
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> int:
    path = os.environ.get("SUPERAGENT_EVENTS_FILE")
    if not path:
        print("SUPERAGENT_EVENTS_FILE is not set", file=sys.stderr)
        return 2
    serve(sys.stdin, sys.stdout, EventLog(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
