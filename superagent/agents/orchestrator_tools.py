"""The orchestrator's tools: their definitions for the model and their implementations.

Reading tools look at the store directly. Acting tools never touch the Engine: they queue a command and wait for the
runtime to apply it (see :mod:`superagent.commands`), so the Engine checks every action exactly as it checks one from
the panel. The same class serves the in-process orchestrator of the ``anthropic`` backend and the MCP bridge of the
``claude-code`` backend, which runs in another process and opens the same SQLite file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from superagent.agents.assessor import OUTPUT_SCHEMA
from superagent.agents.tools import STRING, _tool
from superagent.commands import call, is_error
from superagent.core.engine import MAX_REPLY_WORDS
from superagent.core.model import Assignment, Decision, Thread, Ticket, Trigger
from superagent.core.stage import ticket_stage
from superagent.outbox import Outbox
from superagent.store import Store

WORKERS_KEY = "runtime:workers"
MAX_NOTE_WORDS = 150
SUMMARY_CHARS = 400
MESSAGE_CHARS = 600

_ROUTE_PROPERTIES = {"eval_id": STRING, **OUTPUT_SCHEMA["properties"]}

ORCHESTRATOR_TOOLS: list[dict[str, Any]] = [
    _tool("list_tickets",
          "List tickets across every repository: stage, whether a worker session is running now, pull requests and "
          "the latest report. which is open, ended or all.",
          {"which": {"type": "string", "enum": ["open", "ended", "all"]}}),
    _tool("read_ticket",
          "Read one ticket by id or name: its mandate, the decision that created it, reports, inputs, messages sent "
          "and assignments.",
          {"ticket": STRING}),
    _tool("read_conversation",
          "Read a conversation by its key, such as github:owner/name#12 or inbox:main: its latest messages with what "
          "happened to each, the evaluation in progress, and the tickets that follow it.",
          {"thread": STRING}),
    _tool("route",
          "Decide a candidate by its eval_id: accept (start a ticket), attach (to the ticket in ticket_id), stop (the "
          "ticket in ticket_id, when a person asked), or ignore. Fill every field as the routing instructions say; "
          "use an empty string or an empty list where a field does not apply. The host checks the decision against "
          "the policy and may record it differently; the result says what was recorded.",
          _ROUTE_PROPERTIES),
    _tool("note",
          "Give a ticket's worker a fact from other work, such as another ticket that changes the same files or "
          f"duplicates it. At most {MAX_NOTE_WORDS} words. A note is context: it never widens the ticket's scope. "
          "An idle worker starts a turn to read it.",
          {"ticket": STRING, "text": STRING}),
    _tool("request_status",
          "Ask a ticket's worker to report where it stands. Use it only for a ticket whose state its latest report "
          "does not tell; it starts a worker turn.",
          {"ticket": STRING}),
    _tool("retry",
          "Start a new assignment for an open ticket that waits after a blocked or failed report, or after a failed "
          "delivery.",
          {"ticket": STRING}),
    _tool("stop_ticket",
          "Stop a ticket: its mandate is withdrawn, its worker stops and it receives nothing more. Only when a person "
          "asked for it, or when it duplicates another open ticket.",
          {"ticket": STRING, "reason": STRING}),
    _tool("reevaluate",
          "Send a stored message to evaluation again, as possible new work, by its message id.",
          {"message_id": STRING}),
    _tool("notify",
          f"Send a short message (at most {MAX_REPLY_WORDS} words) to a ticket's conversation. Only for a person who "
          "must act and whom no worker can tell; workers acknowledge, answer and deliver results themselves.",
          {"ticket": STRING, "text": STRING}),
    _tool("set_processing",
          "Pause or resume processing for the whole agent. Only when the operator asks.",
          {"paused": {"type": "boolean"}}),
]

TOOL_NAMES = tuple(t["name"] for t in ORCHESTRATOR_TOOLS)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _time(iso: str) -> str:
    return iso[:16].replace("T", " ") if iso else "-"


Caller = Callable[..., "str | None"]


class OrchestratorTools:
    def __init__(self, store: Store, command_timeout: float = 30.0, caller: Caller = call) -> None:
        self.store = store
        self.command_timeout = command_timeout
        self.caller = caller

    def execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool call. Returns the text the model sees and whether it is an error."""
        if not isinstance(arguments, dict):
            return "the tool input must be a JSON object", True
        handler = getattr(self, f"_tool_{name}", None) if name in TOOL_NAMES else None
        if handler is None:
            return f"unknown tool {name!r}", True
        try:
            return handler(arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return f"invalid input: {exc}", True

    # -- helpers -----------------------------------------------------------------------------------------------

    def _busy(self) -> set[str] | None:
        status = self.store.kv_get(WORKERS_KEY)
        return set(status.get("busy_tickets") or []) if isinstance(status, dict) else None

    def find_ticket(self, ref: str) -> Ticket | None:
        ref = ref.strip()
        if not ref:
            return None
        ticket = self.store.get(Ticket, ref)
        if ticket is not None:
            return ticket
        for tag in ("open", None):
            for candidate in self.store.list(Ticket, tag=tag, newest_first=True):
                if candidate.name == ref:
                    return candidate
        return None

    def _command(self, kind: str, **args: Any) -> tuple[str, bool]:
        outcome = self.caller(self.store, kind, timeout=self.command_timeout, **args)
        if outcome is None:
            return ("the agent did not apply the command in time; it may still run. Check with list_tickets before "
                    "trying again."), True
        return outcome, is_error(outcome)

    def _stage(self, ticket: Ticket) -> str:
        assignment = self.store.get(Assignment, ticket.assignment_id) if ticket.assignment_id else None
        return ticket_stage(ticket, assignment).label

    # -- reading -----------------------------------------------------------------------------------------------

    def _tool_list_tickets(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        which = str(arguments.get("which") or "open")
        tag = None if which == "all" else which
        tickets = self.store.list(Ticket, tag=tag, newest_first=True, limit=100)
        busy = self._busy()
        status = self.store.kv_get(WORKERS_KEY)
        lines = [f"{len(tickets)} {which} tickets."]
        if isinstance(status, dict):
            lines[0] += (f" Worker sessions running: {status.get('running', 0)} of {status.get('concurrency', '?')}, "
                         f"{status.get('queued', 0)} waiting for a slot.")
        for ticket in tickets:
            running = " · worker running now" if busy and ticket.id in busy else ""
            lines.append(f"- {ticket.id} [{ticket.name}] {self._stage(ticket)}{running} · {ticket.origin_thread}")
            lines.append(f"  \"{ticket.title}\" · requested by {ticket.mandate.requester} "
                         f"({ticket.mandate.strength})")
            if ticket.pull_requests:
                closed = ticket.closed_pull_requests
                lines.append("  pull requests: " + ", ".join(
                    f"{key} ({closed[key]})" if key in closed else key for key in ticket.pull_requests))
            last = ticket.last_report
            if last:
                lines.append(f"  latest report ({last.status}, {_time(last.at)}): {_clip(last.summary, SUMMARY_CHARS)}")
            if ticket.attention:
                lines.append(f"  waiting for an answer to: {_clip(ticket.attention, SUMMARY_CHARS)}")
        return "\n".join(lines), False

    def _tool_read_ticket(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        ticket = self.find_ticket(str(arguments.get("ticket") or ""))
        if ticket is None:
            return "no such ticket; list_tickets shows ids and names", True
        busy = self._busy()
        mandate = ticket.mandate
        decision = self.store.get(Decision, ticket.decision_id)
        lines = [
            f"Ticket {ticket.id} [{ticket.name}] \"{ticket.title}\"",
            f"Stage: {self._stage(ticket)}" + (" · worker session running now" if busy and ticket.id in busy else ""),
            f"Conversation: {ticket.origin_thread}",
            f"Mandate: {mandate.scope} ({mandate.strength}, requested by {mandate.requester} on {mandate.source}"
            + (f", rules {', '.join(mandate.rules)}" if mandate.rules else "") + ")",
        ]
        if decision:
            lines.append(f"Accepted because: {decision.reason}")
        if ticket.pull_requests:
            lines.append("Pull requests: " + ", ".join(
                f"{k} ({ticket.closed_pull_requests[k]})" if k in ticket.closed_pull_requests else k
                for k in ticket.pull_requests))
        if not ticket.live:
            lines.append(f"Ended: {ticket.outcome} by {ticket.ended_by}")
        lines.append("Reports, oldest first:")
        lines += [f"- {_time(r.at)} {r.status}: {_clip(r.summary, SUMMARY_CHARS)}"
                  + (f" | question: {_clip(r.question, SUMMARY_CHARS)}" if r.question else "")
                  for r in ticket.reports[-5:]] or ["- none yet"]
        lines.append("Inputs, oldest first:")
        for item in ticket.inputs[-10:]:
            trigger = self.store.get(Trigger, item.trigger_id) if item.trigger_id else None
            text = trigger.text if trigger else item.text
            origin = f"message {trigger.id} from {trigger.actor or 'system'}" if trigger else item.kind
            state = "read" if item.read else ("delivered" if item.delivered else "waiting")
            lines.append(f"- {_time(item.at)} {origin} ({state}{', owned' if item.owned else ''}): "
                         f"{_clip(text, MESSAGE_CHARS)}")
        messages = Outbox(self.store).recent(limit=5, ticket_id=ticket.id)
        lines.append("Messages sent for it, newest first:")
        lines += [f"- {m['kind']} ({m['status']}) to {m['address']}: {_clip(m['text'], SUMMARY_CHARS)}"
                  for m in messages] or ["- none"]
        lines.append(f"Assignments: {len(ticket.assignment_ids)}")
        return "\n".join(lines), False

    def _tool_read_conversation(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        key = str(arguments.get("thread") or "").strip()
        thread = self.store.get(Thread, key)
        if thread is None:
            return "no such conversation; ticket listings show conversation keys", True
        lines = [f"Conversation {thread.key}" + (f" \"{thread.title}\"" if thread.title else "")]
        if thread.eval_id:
            lines.append(f"Being evaluated now: {thread.eval_id}")
        followers = []
        for sub in thread.subscriptions:
            ticket = self.store.get(Ticket, sub.ticket_id)
            if ticket:
                followers.append(f"{ticket.name} ({ticket.id}, {self._stage(ticket)}"
                                 + (", context only" if sub.context_only else "") + ")")
        lines.append("Followed by: " + (", ".join(followers) or "no ticket"))
        lines.append("Latest messages, oldest first:")
        for trigger_id in thread.trigger_ids[-15:]:
            trigger = self.store.get(Trigger, trigger_id)
            if trigger is None:
                continue
            disposition = self.store.kv_get(f"disposition:{trigger.id}") or {}
            what = disposition.get("kind", "")
            detail = disposition.get("detail", "")
            lines.append(f"- {trigger.id} {_time(trigger.at)} {trigger.actor or 'system'} ({trigger.kind})"
                         + (f" [{what}{': ' + detail if detail else ''}]" if what else "")
                         + f": {_clip(trigger.text, MESSAGE_CHARS)}")
        return "\n".join(lines), False

    # -- acting ------------------------------------------------------------------------------------------------

    def _tool_route(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        eval_id = str(arguments.get("eval_id") or "").strip()
        if not eval_id:
            return "eval_id is required", True
        outcome = {k: arguments.get(k) for k in OUTPUT_SCHEMA["properties"] if k in arguments}
        return self._command("decide", eval=eval_id, outcome=outcome)

    def _ticket_command(self, arguments: dict[str, Any], kind: str, **extra: Any) -> tuple[str, bool]:
        ticket = self.find_ticket(str(arguments.get("ticket") or ""))
        if ticket is None:
            return "no such ticket; list_tickets shows ids and names", True
        text, error = self._command(kind, ticket=ticket.id, **extra)
        if error or kind != "note":
            return text, error
        busy = self._busy()
        if ticket.held:
            text += ("; the ticket waits for a person after a blocked or failed report, so its worker reads it after a "
                     "retry or a person's message")
        elif busy and ticket.id in busy:
            text += "; its worker is running and reads it at its next step or turn"
        else:
            text += "; its worker starts a turn to read it"
        return text, False

    def _tool_note(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        text = str(arguments.get("text") or "").strip()
        words = len(text.split())
        if not words:
            return "the note is empty", True
        if words > MAX_NOTE_WORDS:
            return f"the note has {words} words; the limit is {MAX_NOTE_WORDS}", True
        return self._ticket_command(arguments, "note", text=text, note_kind="orchestrator")

    def _tool_request_status(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        return self._ticket_command(arguments, "note", text="", note_kind="status")

    def _tool_retry(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        return self._ticket_command(arguments, "retry")

    def _tool_stop_ticket(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        reason = str(arguments.get("reason") or "").strip()
        if not reason:
            return "say why in reason", True
        return self._ticket_command(arguments, "stop", reason=f"orchestrator: {reason}", by="orchestrator")

    def _tool_reevaluate(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        message_id = str(arguments.get("message_id") or "").strip()
        if not message_id:
            return "message_id is required", True
        return self._command("reevaluate", trigger=message_id)

    def _tool_notify(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        return self._ticket_command(arguments, "notify", text=str(arguments.get("text") or ""))

    def _tool_set_processing(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        return self._command("pause" if arguments.get("paused") else "resume")
