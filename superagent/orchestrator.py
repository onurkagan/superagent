"""The orchestrator in the runtime: when to wake the persistent session, and what it reads when it wakes.

The runtime's main thread calls :meth:`Orchestrator.tick` every second. The orchestrator gathers what happened since
its last turn from the event journal, together with candidates to route and the operator's chat messages. When
something needs it (a candidate, an operator message, a worker report, a ticket that ended, a failed delivery) it waits
``batch_seconds`` for more to arrive, then runs one turn on a background thread with everything as one update. One
turn runs at a time; what arrives meanwhile goes into the next.

The orchestrator acts only through its tools, which queue commands for the Engine. If a turn fails, candidates it held
go to the per-candidate assessor instead, so routing never depends on the orchestrator alone.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from typing import Any

from superagent.agents.assessor import render_candidate
from superagent.agents.orchestrator import OrchestratorSpec, TurnResult
from superagent.core.engine import Candidate
from superagent.core.ids import new_id
from superagent.core.model import Eval, Ticket
from superagent.store import Store

log = logging.getLogger(__name__)

CHAT_KEY = "orchestrator:chat"
STATE_KEY = "orchestrator:state"
CURSOR_KEY = "orchestrator:cursor"
MAX_CHAT = 200
MAX_EVENT_LINES = 60
MAX_OFFERS = 2

CONTEXT_EVENTS = ("ticket.created", "ticket.resumed", "ticket.retried", "trigger.delivered", "trigger.handed_back",
                  "ticket.resume_refused", "policy.changed", "processing.paused", "processing.resumed",
                  "decision.recorded")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def chat_log(store: Store, limit: int = MAX_CHAT) -> list[dict[str, Any]]:
    """The conversation between the operator and the orchestrator, oldest first."""
    entries = store.kv_get(CHAT_KEY, []) or []
    return entries[-limit:]


def wakes(event: dict[str, Any]) -> bool:
    """Whether an event needs the orchestrator, as opposed to being context for its next turn."""
    kind, data = event["type"], event["data"]
    if kind == "ticket.reported":
        return data.get("status") != "progress"
    if kind == "ticket.ended":
        return data.get("by") != "orchestrator"
    return kind == "delivery.failed"


class Orchestrator:
    def __init__(
        self,
        store: Store,
        spec: Callable[[], OrchestratorSpec],
        run_turn: Callable[[OrchestratorSpec, str], TurnResult],
        done: Callable[[tuple[Any, ...]], None],
        *,
        route: bool = True,
        batch_seconds: float = 3.0,
        fallback: Callable[[Candidate], None] = lambda candidate: None,
        workers: Callable[[], dict[str, Any]] = dict,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.spec = spec
        self.run_turn = run_turn
        self.done = done
        self.route = route
        self.batch_seconds = batch_seconds
        self.fallback = fallback
        self.workers = workers
        self.clock = clock
        self.candidates: dict[str, Candidate] = {}
        self.offers: dict[str, int] = {}
        self.operator: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.wake = False
        self.busy = False
        self.due: float | None = None
        self.turns = 0
        self.last_error = ""
        self.last_turn_at = ""
        self.since = ""
        self.thread: threading.Thread | None = None
        if self.store.kv_get(CURSOR_KEY) is None:
            rows = self.store.query("SELECT COALESCE(MAX(seq), 0) AS seq FROM events")
            self.store.kv_set(CURSOR_KEY, int(rows[0]["seq"]))
        self._publish()

    # -- input -------------------------------------------------------------------------------------------------

    def submit(self, candidate: Candidate) -> None:
        """A candidate for the orchestrator to route."""
        self.candidates[candidate.eval.id] = candidate
        self.wake = True

    def operator_message(self, text: str, message_id: str = "") -> str:
        entry = {"id": message_id or new_id("ch"), "role": "operator", "text": text.strip(), "at": _now()}
        self._append_chat(entry)
        self.operator.append(entry)
        self.wake = True
        self._publish()
        return entry["id"]

    def reset(self) -> str:
        if self.busy:
            return "error: the orchestrator is in a turn; reset it when the turn ends"
        spec = self.spec()
        spec.session_path.unlink(missing_ok=True)
        self._append_chat({"id": new_id("ch"), "role": "system", "text": "The orchestrator's session was reset.",
                           "at": _now()})
        return "the next turn starts a new session"

    # -- the loop ----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        self._read_journal()
        for eval_id in [e for e in self.candidates if self.store.get(Eval, e) is None]:
            self.candidates.pop(eval_id)
            self.offers.pop(eval_id, None)
        if self.busy or not self.wake:
            return
        now = self.clock()
        if self.due is None:
            self.due = now + self.batch_seconds
        if now < self.due:
            return
        self._start()

    def _read_journal(self) -> None:
        cursor = int(self.store.kv_get(CURSOR_KEY, 0) or 0)
        rows = self.store.query("SELECT seq, at, type, data FROM events WHERE seq > ? ORDER BY seq LIMIT 500", (cursor,))
        if not rows:
            return
        for row in rows:
            event = {"seq": row["seq"], "at": row["at"], "type": row["type"], "data": json.loads(row["data"])}
            if wakes(event):
                self.wake = True
            elif event["type"] not in CONTEXT_EVENTS:
                continue
            line = self._render_event(event)
            if line:
                self.events.append(line)
        self.events = self.events[-MAX_EVENT_LINES:]
        self.store.kv_set(CURSOR_KEY, int(rows[-1]["seq"]))

    def _name(self, ticket_id: str) -> str:
        ticket = self.store.get(Ticket, ticket_id) if ticket_id else None
        return f"{ticket.name} ({ticket_id})" if ticket else ticket_id or "-"

    def _render_event(self, event: dict[str, Any]) -> str:
        kind, data, at = event["type"], event["data"], event["at"][11:16]
        ticket = self._name(str(data.get("ticket") or ""))
        if kind == "ticket.reported":
            if data.get("status") == "progress":
                return ""
            return f"- {at} {ticket} reported {data.get('status')}: {data.get('summary', '')}"
        if kind == "ticket.ended":
            reason = f": {data['reason']}" if data.get("reason") else ""
            return f"- {at} {ticket} ended {data.get('outcome')} (by {data.get('by')}){reason}"
        if kind == "ticket.created":
            return f"- {at} ticket {ticket} was created ({data.get('strength')})"
        if kind == "ticket.resumed":
            return f"- {at} {ticket} resumed because of message {data.get('trigger')}"
        if kind == "ticket.retried":
            return f"- {at} {ticket} was retried"
        if kind == "ticket.resume_refused":
            return f"- {at} {ticket} could not resume: its mandate no longer holds"
        if kind == "trigger.delivered":
            names = ", ".join(self._name(t) for t in data.get("tickets") or [])
            return f"- {at} message {data.get('trigger')} went straight to {names}, which own it"
        if kind == "trigger.handed_back":
            return f"- {at} {ticket} handed back message {data.get('trigger')}: {data.get('reason', '')}"
        if kind == "delivery.failed":
            return f"- {at} delivering input to {ticket} failed: {data.get('error', '')}"
        if kind == "decision.recorded":
            target = f" -> {self._name(str(data.get('ticket') or ''))}" if data.get("ticket") else ""
            return f"- {at} decision {data.get('kind')} on {data.get('thread')}{target}"
        if kind == "policy.changed":
            return f"- {at} the policy changed to {data.get('current')}"
        return f"- {at} {kind.replace('.', ' ')}"

    def _update(self, candidates: list[Candidate]) -> str:
        parts = [f'<update at="{_now()}">']
        for entry in self.operator:
            parts.append(f'<operator id="{entry["id"]}" at="{entry["at"]}">\n{escape(entry["text"], quote=False)}\n'
                         "</operator>")
        for candidate in candidates:
            if self.offers.get(candidate.eval.id, 0) > 1:
                parts.append(f"This candidate is still undecided; route {candidate.eval.id} now.")
            parts.append(render_candidate(candidate))
        if self.events:
            parts.append("<events>\n" + "\n".join(self.events) + "\n</events>")
        workers = self.workers()
        if workers:
            busy = ", ".join(self._name(t) for t in workers.get("busy_tickets") or []) or "none"
            parts.append(f"Worker sessions running: {workers.get('running', 0)} of {workers.get('concurrency', '?')}"
                         f" ({busy}); {workers.get('queued', 0)} waiting for a slot.")
        parts.append("</update>")
        return "\n".join(parts)

    def _start(self) -> None:
        candidates = list(self.candidates.values()) if self.route else []
        for candidate in candidates:
            self.offers[candidate.eval.id] = self.offers.get(candidate.eval.id, 0) + 1
        self.candidates.clear()
        update = self._update(candidates)
        operator_ids = [e["id"] for e in self.operator]
        eval_ids = [c.eval.id for c in candidates]
        held = {c.eval.id: c for c in candidates}
        self.operator, self.events, self.wake, self.due = [], [], False, None
        self.busy, self.since = True, _now()
        self._publish()
        spec = self.spec()

        def run() -> None:
            try:
                result = self.run_turn(spec, update)
            except Exception as exc:  # noqa: BLE001 - a crashed turn is reported like a failed one
                log.exception("orchestrator turn crashed")
                result = TurnResult("", f"the turn crashed: {exc}")
            self.done(("orchestrated", result, operator_ids, eval_ids, held))

        self.thread = threading.Thread(target=run, name="orchestrator", daemon=True)
        self.thread.start()

    def finished(self, result: TurnResult, operator_ids: list[str], eval_ids: list[str],
                 held: dict[str, Candidate]) -> None:
        """Called on the main thread when a turn ends."""
        self.busy = False
        self.turns += 1
        self.last_turn_at = _now()
        self.last_error = result.error
        text = result.text.strip() if not result.error else f"The turn failed: {result.error}"
        if result.error:
            log.warning("orchestrator turn failed: %s", result.error)
        self._append_chat({"id": new_id("ch"), "role": "orchestrator", "text": text or "(no answer)",
                           "at": _now(), "reply_to": operator_ids, "error": bool(result.error)})
        for eval_id in eval_ids:
            if self.store.get(Eval, eval_id) is None:
                self.offers.pop(eval_id, None)
            elif result.error or self.offers.get(eval_id, 0) >= MAX_OFFERS:
                self.offers.pop(eval_id, None)
                self.fallback(held[eval_id])
            else:
                self.candidates[eval_id] = held[eval_id]
                self.wake = True
        self._publish()

    def wait(self, timeout: float = 5.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    # -- state -------------------------------------------------------------------------------------------------

    def _append_chat(self, entry: dict[str, Any]) -> None:
        entries = self.store.kv_get(CHAT_KEY, []) or []
        entries.append(entry)
        self.store.kv_set(CHAT_KEY, entries[-MAX_CHAT:])

    def state(self) -> dict[str, Any]:
        return {"enabled": True, "route": self.route, "busy": self.busy, "since": self.since if self.busy else "",
                "turns": self.turns, "last_turn_at": self.last_turn_at, "last_error": self.last_error,
                "waiting": {"candidates": len(self.candidates), "operator": len(self.operator),
                            "events": len(self.events)}}

    def _publish(self) -> None:
        self.store.kv_set(STATE_KEY, self.state())
