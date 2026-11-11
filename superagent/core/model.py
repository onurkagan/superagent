"""Domain records. See docs/001-domain.md for what each concept means and which rules it keeps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Strength = Literal["explicit", "inferred"]
DecisionKind = Literal["accept", "attach", "stop", "ignore", "unavailable"]
ReportStatus = Literal["progress", "waiting", "ready", "blocked", "failed", "dropped"]
AssignmentState = Literal["pending", "running", "ended"]
TicketState = Literal["open", "ended"]

REPORT_STATUSES: tuple[str, ...] = ("progress", "waiting", "ready", "blocked", "failed", "dropped")
TERMINAL_REPORTS: frozenset[str] = frozenset({"waiting", "ready", "blocked", "failed", "dropped"})


@dataclass(frozen=True)
class ChannelRef:
    """Where a reply goes: a conversation on one Source, optionally answering one message in it."""

    source: str
    address: str
    reply_to: str = ""


@dataclass(frozen=True)
class Trigger:
    """One immutable event from a Source.

    ``addressed`` is ``"mention"`` or ``"direct"`` when the message speaks to the agent, else empty. ``observed`` is true
    when the Source received the message only because it receives every message in ``scope``.
    """

    id: str
    source: str
    kind: str
    actor: str
    text: str
    threads: tuple[str, ...]
    channels: tuple[ChannelRef, ...] = ()
    actor_is_bot: bool = False
    addressed: str = ""
    scope: str = ""
    observed: bool = False
    url: str = ""
    title: str = ""
    at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_person(self) -> bool:
        return bool(self.actor) and not self.actor_is_bot


@dataclass
class Subscription:
    """A Ticket following a Thread. A context-only Subscription delivers messages but grants no ownership."""

    ticket_id: str
    context_only: bool = False


@dataclass
class Thread:
    key: str
    source: str
    title: str = ""
    trigger_ids: list[str] = field(default_factory=list)
    subscriptions: list[Subscription] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    handbacks: dict[str, list[str]] = field(default_factory=dict)
    eval_id: str = ""
    eval_retries: int = 0
    decision_ids: list[str] = field(default_factory=list)
    updated_at: str = ""

    def subscription(self, ticket_id: str) -> Subscription | None:
        return next((s for s in self.subscriptions if s.ticket_id == ticket_id), None)


@dataclass
class Eval:
    """A running judgement on a batch of Triggers from one Thread."""

    id: str
    thread_key: str
    trigger_ids: list[str]
    strength: Strength
    basis: str
    cell: str
    requester: str
    source: str
    policy_version: str
    started_at: str
    deadline: str


@dataclass
class Decision:
    id: str
    eval_id: str
    thread_key: str
    kind: DecisionKind
    reason: str
    trigger_ids: list[str]
    strength: Strength
    policy_version: str
    ticket_id: str = ""
    rules: list[str] = field(default_factory=list)
    would_accept: bool = False
    at: str = ""


@dataclass(frozen=True)
class Mandate:
    """What an accept grants. Immutable: wider scope means a new Ticket."""

    policy_version: str
    strength: Strength
    basis: str
    scope: str
    requester: str
    source: str
    rules: tuple[str, ...] = ()
    cell: str = ""


@dataclass
class Input:
    """Context delivered to a Ticket's Executor: a Trigger, a note, a Policy change, a resume or a recovery."""

    id: str
    kind: str
    text: str = ""
    trigger_id: str = ""
    owned: bool = False
    delivered: bool = False
    read: bool = False
    at: str = ""


@dataclass
class Report:
    """The state an Assignment reports to its Ticket. ``question`` is one concrete question for a person."""

    status: ReportStatus
    summary: str
    question: str = ""
    question_trigger: str = ""
    pull_request: str = ""
    at: str = ""


@dataclass
class Assignment:
    id: str
    ticket_id: str
    state: AssignmentState = "pending"
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    end_reason: str = ""


@dataclass
class Ticket:
    """A unit of authorized work."""

    id: str
    name: str
    title: str
    mandate: Mandate
    decision_id: str
    origin_thread: str
    origin_trigger: str
    channels: list[ChannelRef] = field(default_factory=list)
    state: TicketState = "open"
    outcome: str = ""
    ended_by: str = ""
    inputs: list[Input] = field(default_factory=list)
    assignment_id: str = ""
    assignment_ids: list[str] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    attention: str = ""
    pull_requests: list[str] = field(default_factory=list)
    opened_pull_requests: list[str] = field(default_factory=list)
    closed_pull_requests: dict[str, str] = field(default_factory=dict)
    delivery_failures: int = 0
    human_since_report: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def live(self) -> bool:
        return self.state == "open"

    @property
    def stopped(self) -> bool:
        """Stopped by a person: the Mandate is withdrawn and the Ticket receives nothing more."""
        return self.state == "ended" and self.outcome == "stopped"

    @property
    def last_report(self) -> Report | None:
        return self.reports[-1] if self.reports else None

    def undelivered(self) -> list[Input]:
        return [i for i in self.inputs if not i.delivered]

    @property
    def held(self) -> bool:
        """A blocked or failed Ticket takes no automatic Input until a person writes or someone retries."""
        last = self.last_report
        return bool(last and last.status in ("blocked", "failed") and not self.human_since_report)
