"""Read models for the panel and the CLI.

Every view answers one of three questions, in this order: does anything need me now, is the system healthy and doing
the right things, and what is the history of one piece of work. Counts equal the length of the lists they link to.
See docs/005-panel.md.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from superagent.core.model import Assignment, Decision, Thread, Ticket, Trigger
from superagent.core.serde import to_dict
from superagent.core.stage import ticket_stage
from superagent.outbox import Outbox
from superagent.store import Store

COMMANDS = ("stop", "retry", "pause", "resume", "reevaluate")


def _since(hours: int = 24) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="milliseconds")


class PanelData:
    def __init__(self, store: Store, runtime_status: Callable[[], dict[str, Any]] | None = None) -> None:
        self.store = store
        self.outbox = Outbox(store)
        self.runtime_status = runtime_status

    # -- tickets -----------------------------------------------------------------------------------------------

    def _row(self, ticket: Ticket) -> dict[str, Any]:
        assignment = self.store.get(Assignment, ticket.assignment_id) if ticket.assignment_id else None
        stage = ticket_stage(ticket, assignment)
        last = ticket.last_report
        return {
            "id": ticket.id,
            "name": ticket.name,
            "title": ticket.title,
            "state": ticket.state,
            "outcome": ticket.outcome,
            "stage": {"group": stage.group, "name": stage.name, "label": stage.label},
            "requester": ticket.mandate.requester,
            "strength": ticket.mandate.strength,
            "thread": ticket.origin_thread,
            "attention": ticket.attention,
            "last_report": to_dict(last) if last else None,
            "pull_requests": ticket.pull_requests,
            "can_retry": ticket.live and stage.name in ("delivery", "blocked", "failed"),
            "updated_at": ticket.updated_at,
        }

    def tickets(self, which: str = "open", limit: int = 200) -> list[dict[str, Any]]:
        if which == "open":
            items = self.store.list(Ticket, tag="open", newest_first=True, limit=limit)
        elif which == "ended":
            items = self.store.list(Ticket, tag="ended", newest_first=True, limit=limit)
        else:
            items = self.store.list(Ticket, newest_first=True, limit=limit)
        return [self._row(t) for t in items]

    def ticket(self, ticket_id: str) -> dict[str, Any] | None:
        ticket = self.store.get(Ticket, ticket_id)
        if ticket is None:
            return None
        decision = self.store.get(Decision, ticket.decision_id)
        inputs = []
        for item in ticket.inputs:
            trigger = self.store.get(Trigger, item.trigger_id) if item.trigger_id else None
            inputs.append({**to_dict(item), "from": trigger.actor if trigger else "",
                           "text": (trigger.text if trigger else item.text)[:2000],
                           "url": trigger.url if trigger else ""})
        assignments = [to_dict(a) for a in (self.store.get(Assignment, i) for i in ticket.assignment_ids) if a]
        return {
            **self._row(ticket),
            "mandate": to_dict(ticket.mandate),
            "decision": to_dict(decision) if decision else None,
            "channels": to_dict(ticket.channels),
            "reports": to_dict(ticket.reports),
            "inputs": inputs,
            "assignments": assignments,
            "messages": self.outbox.recent(limit=50, ticket_id=ticket.id),
        }

    # -- the three questions -----------------------------------------------------------------------------------

    def needs_person(self) -> list[dict[str, Any]]:
        return [row for row in self.tickets("open") if row["stage"]["group"] == "person"]

    def problems(self) -> list[dict[str, Any]]:
        problems: list[dict[str, Any]] = []
        for message in self.outbox.problems():
            problems.append({"kind": "message", "detail": f"A message to {message['source']} {message['address']} "
                                                          f"failed {message['attempts']} times: {message['last_error']}",
                             "ticket": message["ticket_id"]})
        status = self.runtime_status() if self.runtime_status else {}
        for name, health in (status.get("sources") or {}).items():
            if not health.get("ok", True):
                problems.append({"kind": "source", "detail": f"{name}: {health.get('detail', '')}", "ticket": ""})
        since = _since()
        for decision in self.store.list(Decision, newest_first=True, limit=200):
            if decision.at < since:
                break
            if decision.kind != "unavailable":
                continue
            thread = self.store.get(Thread, decision.thread_key)
            if thread and thread.decision_ids and thread.decision_ids[-1] == decision.id \
                    and not thread.queued and not thread.eval_id:
                problems.append({"kind": "eval", "detail": f"Could not judge {decision.thread_key}: {decision.reason}",
                                 "ticket": "", "triggers": decision.trigger_ids})
        return problems

    def activity(self) -> dict[str, int]:
        since = _since()

        def count(kind: str) -> int:
            rows = self.store.query("SELECT COUNT(*) AS n FROM docs WHERE kind = ? AND updated_at >= ?", (kind, since))
            return int(rows[0]["n"])

        decisions = self.store.query(
            "SELECT json_extract(body, '$.kind') AS k, COUNT(*) AS n FROM docs WHERE kind = 'decision' "
            "AND updated_at >= ? GROUP BY k", (since,))
        sent = self.store.query("SELECT COUNT(*) AS n FROM messages WHERE status = 'sent' AND sent_at >= ?", (since,))
        created = self.store.query("SELECT COUNT(*) AS n FROM events WHERE type = 'ticket.created' AND at >= ?",
                                   (since,))
        ended = self.store.query("SELECT COUNT(*) AS n FROM events WHERE type = 'ticket.ended' AND at >= ?", (since,))
        result = {"triggers": count("trigger"), "tickets_created": int(created[0]["n"]),
                  "tickets_ended": int(ended[0]["n"]), "messages_sent": int(sent[0]["n"])}
        for row in decisions:
            result[f"decisions_{row['k']}"] = int(row["n"])
        return result

    def overview(self) -> dict[str, Any]:
        status = self.runtime_status() if self.runtime_status else {}
        return {
            "needs_person": self.needs_person(),
            "problems": self.problems(),
            "activity": self.activity(),
            "paused": bool(self.store.kv_get("paused", False)),
            "executor": status.get("executor"),
            "policy": status.get("policy", ""),
            "open_tickets": self.store.count(Ticket, tag="open"),
        }

    # -- history -----------------------------------------------------------------------------------------------

    def decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        return [to_dict(d) for d in self.store.list(Decision, newest_first=True, limit=limit)]

    def threads(self, which: str = "subscribed", limit: int = 100) -> list[dict[str, Any]]:
        threads = self.store.list(Thread)
        if which == "subscribed":
            threads = [t for t in threads if t.subscriptions]
        elif which == "evaluating":
            threads = [t for t in threads if t.queued or t.eval_id]
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        rows = []
        for thread in threads[:limit]:
            triggers = []
            for trigger_id in reversed(thread.trigger_ids[-20:]):
                trigger = self.store.get(Trigger, trigger_id)
                if trigger is None:
                    continue
                triggers.append({"id": trigger.id, "actor": trigger.actor, "kind": trigger.kind,
                                 "text": trigger.text[:500], "url": trigger.url, "at": trigger.at,
                                 "disposition": self.store.kv_get(f"disposition:{trigger.id}")})
            rows.append({"key": thread.key, "title": thread.title, "queued": thread.queued,
                         "evaluating": bool(thread.eval_id), "subscriptions": to_dict(thread.subscriptions),
                         "decisions": thread.decision_ids[-10:], "triggers": triggers, "updated_at": thread.updated_at})
        return rows

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.events(limit=limit)

    # -- commands ----------------------------------------------------------------------------------------------

    def command(self, kind: str, **args: Any) -> None:
        if kind not in COMMANDS:
            raise ValueError(f"unknown command {kind!r}")
        self.store.enqueue_command(kind, **args)
