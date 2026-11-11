"""One worker turn: the model loop that works on a Ticket until it reports.

The conversation is stored in a session file per Ticket, so later turns, retries and restarts continue the same
history. New Inputs that arrive while a turn runs are taken in at the next step, which lets a person redirect a
worker without waiting for it to finish. The loop is written by hand rather than with the SDK's tool runner because
it injects Inputs between steps and persists every step.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from superagent.agents.tools import TOOL_DEFINITIONS, ToolContext, WorkerHost, execute
from superagent.core.model import Report
from superagent.llm.client import ModelClient, ModelError, TransientModelError

RETRY_DELAYS_SECONDS = (5.0, 20.0, 60.0)
CUT_OFF_NOTE = "Your previous response hit the output limit and was cut off. Continue, in smaller steps."


@dataclass
class WorkerSpec:
    """Everything a worker turn needs, built by the runtime from the store."""

    ticket_id: str
    assignment_id: str
    name: str
    directory: Path
    session_path: Path
    system: str
    model: str
    effort: str = "high"
    env: dict[str, str] = field(default_factory=dict)
    blocked_commands: tuple[str, ...] = ()
    max_steps: int = 200
    max_tokens: int = 16000


class WorkerEvents(Protocol):
    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None: ...

    def progress(self, ticket_id: str, assignment_id: str, report: Report) -> None: ...


class Session:
    """The stored message history of one Ticket's worker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[dict[str, Any]] = []
        if path.is_file():
            self.messages = json.loads(path.read_text(encoding="utf-8")).get("messages", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"messages": self.messages}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add_user_text(self, texts: list[str]) -> None:
        """Add texts to the conversation, merging into a trailing user message (such as tool results)."""
        blocks = [{"type": "text", "text": t} for t in texts if t.strip()]
        if not blocks:
            return
        last = self.messages[-1] if self.messages else None
        if last is not None and last["role"] == "user":
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].extend(blocks)
        else:
            self.messages.append({"role": "user", "content": blocks})

    @property
    def awaiting_model(self) -> bool:
        return bool(self.messages) and self.messages[-1]["role"] == "user"


def run_turn(
    spec: WorkerSpec,
    client: ModelClient,
    inputs: list[tuple[str, str]],
    events: WorkerEvents,
    host: WorkerHost | None = None,
    inbox: Callable[[], list[tuple[str, str]]] = lambda: [],
    cancelled: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> Report | None:
    """Run the model until it reports a terminal status. Returns that Report, or None when cancelled."""
    session = Session(spec.session_path)
    ctx = ToolContext(directory=spec.directory, env=spec.env, blocked_commands=spec.blocked_commands, host=host,
                      cancelled=cancelled)
    pending = list(inputs)
    steps = 0
    failures = 0
    while True:
        if cancelled():
            return None
        pending.extend(inbox())
        if pending:
            session.add_user_text([text for _, text in pending])
            session.save()
            events.inputs_read(spec.ticket_id, [input_id for input_id, _ in pending])
            pending = []
        if not session.awaiting_model:
            return Report("waiting", "nothing new to work on")
        try:
            response = client.create(model=spec.model, system=spec.system, messages=session.messages,
                                     tools=TOOL_DEFINITIONS, effort=spec.effort, max_tokens=spec.max_tokens)
        except TransientModelError as exc:
            if failures >= len(RETRY_DELAYS_SECONDS):
                return Report("failed", f"The model API kept failing: {exc}")
            sleep(RETRY_DELAYS_SECONDS[failures])
            failures += 1
            continue
        except ModelError as exc:
            return Report("failed", f"The model call failed: {exc}")
        failures = 0
        steps += 1
        if response.stop_reason == "refusal":
            category = response.refusal_category or "no category"
            return Report("failed", f"The model declined to continue this ticket ({category}).")
        session.messages.append({"role": "assistant", "content": response.content})
        session.save()
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = response.tool_uses()
        if not tool_uses:
            if response.stop_reason == "max_tokens":
                session.add_user_text([CUT_OFF_NOTE])
                session.save()
                continue
            return Report("waiting", response.text()[:1000] or "The turn ended without a report.")
        final = _run_tools(spec, ctx, session, tool_uses, events, cut_off=response.stop_reason == "max_tokens")
        if final is not None:
            return final
        if steps >= spec.max_steps:
            return Report("blocked", f"Stopped after {spec.max_steps} model steps without finishing; "
                                     "a person should look at this ticket.")


def _run_tools(spec: WorkerSpec, ctx: ToolContext, session: Session, tool_uses: list[dict[str, Any]],
               events: WorkerEvents, cut_off: bool) -> Report | None:
    results = []
    final: Report | None = None
    for use in tool_uses:
        if cut_off:
            content, is_error, report = "This call was cut off at the output limit; make it again with less input.", \
                True, None
        else:
            result = execute(use["name"], use.get("input") or {}, ctx)
            content, is_error, report = result.content, result.is_error, result.report
        if report is not None:
            if report.status == "progress":
                events.progress(spec.ticket_id, spec.assignment_id, report)
            elif final is None:
                final = report
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use["id"], "content": content}
        if is_error:
            block["is_error"] = True
        results.append(block)
    session.messages.append({"role": "user", "content": results})
    session.save()
    return final
