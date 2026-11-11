"""The assessor: one structured model call that proposes a Decision for an Eval.

The assessor only proposes. The Engine checks the proposal against the gate strength and the Policy, so a model
cannot accept work the Policy does not allow, invent rules, or stop a ticket on an inferred request.
"""

from __future__ import annotations

import json
from typing import Any

from superagent.agents.prompts import PromptLibrary
from superagent.core.engine import Candidate, Outcome
from superagent.core.model import Ticket, Trigger
from superagent.core.policy import Policy
from superagent.llm.client import ModelClient, ModelError

MAX_MESSAGE_CHARS = 1500

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "attach", "stop", "ignore"]},
        "reason": {"type": "string"},
        "title": {"type": "string"},
        "scope": {"type": "string"},
        "name": {"type": "string"},
        "acknowledgement": {"type": "string"},
        "note": {"type": "string"},
        "ticket_id": {"type": "string"},
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "matches": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["id", "matches", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "reason", "title", "scope", "name", "acknowledgement", "note", "ticket_id", "rules"],
    "additionalProperties": False,
}


def _clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def render_message(trigger: Trigger) -> str:
    who = "bot" if trigger.actor_is_bot else ("person" if trigger.actor else "system")
    attrs = [f'id="{trigger.id}"', f'from="{trigger.actor or "-"}"', f'sender="{who}"', f'kind="{trigger.kind}"']
    if trigger.addressed:
        attrs.append(f'addressed="{trigger.addressed}"')
    if trigger.at:
        attrs.append(f'at="{trigger.at}"')
    return f"<message {' '.join(attrs)}>\n{_clip(trigger.text)}\n</message>"


def render_ticket(ticket: Ticket) -> str:
    state = "open" if ticket.live else f"ended ({ticket.outcome})"
    return f'- {ticket.id} "{ticket.title}" [{ticket.name}], {state}; scope: {ticket.mandate.scope}'


def render_candidate(candidate: Candidate) -> str:
    evaluation = candidate.eval
    thread = candidate.thread
    parts = [
        f'<candidate eval="{evaluation.id}" strength="{evaluation.strength}" basis="{evaluation.basis}">',
        f"Conversation: {thread.key}" + (f' "{thread.title}"' if thread.title else ""),
    ]
    if candidate.context:
        parts.append("\nEarlier messages, oldest first, for context only:")
        parts += [render_message(t) for t in candidate.context]
    parts.append("\nNew messages to judge:")
    parts += [render_message(t) for t in candidate.triggers]
    if candidate.handbacks:
        parts.append("\nWorkers returned some of these messages:")
        for trigger_id, reasons in candidate.handbacks.items():
            parts.append(f"- {trigger_id}: " + "; ".join(reasons))
    parts.append("\nTickets on this conversation:")
    parts += [render_ticket(t) for t in candidate.tickets] or ["- none"]
    parts.append("</candidate>")
    return "\n".join(parts)


class Assessor:
    def __init__(self, client: ModelClient, prompts: PromptLibrary, model: str, effort: str = "low") -> None:
        self.client = client
        self.prompts = prompts
        self.model = model
        self.effort = effort

    def assess(self, candidate: Candidate, policy: Policy) -> Outcome:
        """Return the proposed Outcome; any failure becomes ``unavailable`` so the Engine can retry."""
        try:
            response = self.client.create(
                model=self.model,
                system=self.prompts.assessor_system(policy),
                messages=[{"role": "user", "content": render_candidate(candidate)}],
                effort=self.effort,
                max_tokens=8000,
                output_schema=OUTPUT_SCHEMA,
            )
        except ModelError as exc:
            return Outcome("unavailable", f"assessment failed: {exc}")
        if response.stop_reason == "refusal":
            return Outcome("unavailable", f"the model declined the assessment ({response.refusal_category or 'no category'})")
        if response.stop_reason == "max_tokens":
            return Outcome("unavailable", "the assessment was cut off")
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            return Outcome("unavailable", "the assessment was not valid JSON")
        return to_outcome(data)


def to_outcome(data: dict[str, Any]) -> Outcome:
    decision = str(data.get("decision", "ignore"))
    if decision not in ("accept", "attach", "stop", "ignore"):
        decision = "ignore"
    rules = tuple(str(r.get("id", "")) for r in data.get("rules") or [] if isinstance(r, dict) and r.get("matches"))
    return Outcome(
        kind=decision,
        reason=str(data.get("reason", "")).strip()[:500],
        title=str(data.get("title", "")).strip(),
        scope=str(data.get("scope", "")).strip(),
        name=str(data.get("name", "")).strip(),
        acknowledgement=str(data.get("acknowledgement", "")).strip(),
        note=" ".join(str(data.get("note", "")).split()[:30]),
        rules=tuple(r for r in rules if r),
        ticket_id=str(data.get("ticket_id", "")).strip(),
    )
