"""Cards: how Assignments and Inputs read to a worker.

A card carries the original message, not a summary, so the worker judges the request from its source. Message ids
on cards are what the worker passes to ``reply``, ``report`` and ``hand_back``.
"""

from __future__ import annotations

from html import escape

from superagent.core.model import Assignment, Input, Ticket, Trigger
from superagent.core.policy import Policy

MAX_CARD_CHARS = 20000


def _attr(value: str) -> str:
    return escape(value, quote=True)


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_CARD_CHARS:
        return text
    return text[:MAX_CARD_CHARS] + f"\n... [{len(text) - MAX_CARD_CHARS} more characters; read the source link]"


def assignment_card(ticket: Ticket, assignment: Assignment, policy: Policy) -> tuple[str, str]:
    mandate = ticket.mandate
    lines = [
        f'<input id="{_attr(assignment.id)}:card" kind="assignment">',
        f"Ticket: {ticket.title} (name {ticket.name}, id {ticket.id})",
        f"Scope: {mandate.scope}",
        f"Requested by: {mandate.requester} on {mandate.source} ({mandate.strength} request)",
        f"Started on conversation: {ticket.origin_thread}",
    ]
    if mandate.rules:
        names = [f"{r} ({rule.name})" if (rule := policy.rule(r)) else r for r in mandate.rules]
        lines.append("Accepted under the rules: " + ", ".join(names))
    if ticket.acknowledged:
        lines.append("The request was already acknowledged for you; do not acknowledge it again.")
    if len(ticket.assignment_ids) > 1:
        lines.append("This is a new assignment for an existing ticket. Check the directory, the git history and the "
                     "conversation before repeating any outside action.")
    lines.append("</input>")
    return f"{assignment.id}:card", "\n".join(lines)


def input_card(item: Input, trigger: Trigger | None) -> str:
    if item.kind != "trigger" or trigger is None:
        return f'<input id="{_attr(item.id)}" kind="{_attr(item.kind)}">\n{_clip(item.text)}\n</input>'
    sender = "bot" if trigger.actor_is_bot else ("person" if trigger.actor else "system")
    attrs = [
        f'id="{_attr(item.id)}"',
        'kind="message"',
        f'message_id="{_attr(trigger.id)}"',
        f'owned="{str(item.owned).lower()}"',
        f'from="{_attr(trigger.actor or "-")}"',
        f'sender="{sender}"',
        f'type="{_attr(trigger.kind)}"',
        f'conversation="{_attr(trigger.threads[0] if trigger.threads else "")}"',
    ]
    if trigger.addressed:
        attrs.append(f'addressed="{_attr(trigger.addressed)}"')
    if trigger.url:
        attrs.append(f'url="{_attr(trigger.url)}"')
    if trigger.at:
        attrs.append(f'at="{_attr(trigger.at)}"')
    title = f"Title: {trigger.title}\n" if trigger.title and trigger.kind in ("issue", "pull_request") else ""
    return f"<input {' '.join(attrs)}>\n{title}{_clip(trigger.text)}\n</input>"
