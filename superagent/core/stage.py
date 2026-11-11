"""Ticket stages: what an open Ticket waits for, derived from stored facts only.

The rows are checked top to bottom and the first match wins, so the stages are mutually exclusive and cover every
open Ticket. See docs/003-execution.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from superagent.core.model import Assignment, Ticket

OUTCOME_LABELS = {
    "completed": "Completed",
    "dropped": "Nothing to do",
    "stopped": "Stopped by a person",
    "revoked": "Mandate revoked",
    "merged": "Pull request merged",
    "closed": "Pull request closed",
    "failed": "Failed",
}


@dataclass(frozen=True)
class Stage:
    group: str
    name: str
    label: str

    @property
    def needs_person(self) -> bool:
        return self.group == "person"


def _still_ready(ticket: Ticket) -> bool:
    """A worker that reported ``ready`` with a pull request and later only ``waiting`` (after a note or a status
    request, say) still has that pull request ready for review."""
    last = ticket.last_report
    open_pull_requests = [k for k in ticket.pull_requests if k not in ticket.closed_pull_requests]
    return bool(last and last.status == "waiting" and open_pull_requests
                and any(r.status == "ready" for r in ticket.reports))


def ticket_stage(ticket: Ticket, assignment: Assignment | None) -> Stage:
    if not ticket.live:
        return Stage("ended", ticket.outcome or "ended", OUTCOME_LABELS.get(ticket.outcome, "Ended"))
    if ticket.delivery_failures > 0:
        return Stage("person", "delivery", "Input undelivered")
    if ticket.attention:
        return Stage("person", "answer", "Awaiting answer")
    last = ticket.last_report
    if last and (last.status == "ready" or _still_ready(ticket)):
        return Stage("person", "review", "Awaiting review")
    if ticket.held and last:
        return Stage("person", last.status, "Blocked" if last.status == "blocked" else "Failed")
    if assignment is None or assignment.state == "pending":
        return Stage("working", "queued", "Queued")
    if (last and last.status == "waiting") or assignment.state == "ended":
        return Stage("idle", "idle", "Waiting for a trigger")
    return Stage("working", "working", "Working")
