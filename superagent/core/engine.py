"""The Engine: every change to Threads, Evals, Decisions and Tickets goes through it.

The Engine is synchronous and single-threaded; the runtime calls it from its main loop. It never calls a model, a
Source or the Executor directly. Commands that need outside work leave it in the store (pending Assignments,
undelivered Inputs, queued messages) and the runtime picks it up, so a crash between a decision and its effect loses
nothing. See docs/001-domain.md to docs/003-execution.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from superagent.core.ids import new_id, slug, unique_name
from superagent.core.model import (
    Assignment,
    ChannelRef,
    Decision,
    Eval,
    Input,
    Mandate,
    Report,
    Subscription,
    Thread,
    Ticket,
    Trigger,
)
from superagent.core.policy import Policy
from superagent.outbox import OutboundMessage, Outbox
from superagent.store import Store

MAX_EVAL_RETRIES = 2
MAX_REPLY_WORDS = 150
REPLY_PURPOSES = ("acknowledgement", "answer", "result")

RECOVERY_NOTE = (
    "The host restarted while this assignment was running. Before repeating any outside action such as a push, "
    "a comment or a pull request, check the ticket directory, its git history and the conversation for what "
    "already happened."
)
RESUME_NOTE = "A person's message resumed this ticket after it had ended. Decide whether it fits the ticket's scope."


@dataclass(frozen=True)
class Outcome:
    """What an assessment proposes for an Eval. The Engine checks it against the gate and the Policy."""

    kind: str
    reason: str
    title: str = ""
    scope: str = ""
    name: str = ""
    acknowledgement: str = ""
    note: str = ""
    rules: tuple[str, ...] = ()
    ticket_id: str = ""


@dataclass(frozen=True)
class Candidate:
    """The bounded facts an assessment receives. It holds no authority."""

    eval: Eval
    thread: Thread
    triggers: list[Trigger]
    context: list[Trigger]
    tickets: list[Ticket]
    handbacks: dict[str, list[str]] = field(default_factory=dict)


def _utc() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


class Engine:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        outbox: Outbox | None = None,
        *,
        clock: Callable[[], datetime] = _utc,
        listens: Callable[[str], bool] | None = None,
        resolve_thread: Callable[[str], str | None] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.outbox = outbox or Outbox(store)
        self.clock = clock
        self.listens = listens or (lambda thread_key: True)
        self.resolve_thread = resolve_thread or (lambda ref: None)
        self.stopped_assignments: list[tuple[str, str]] = []

    # -- helpers -----------------------------------------------------------------------------------------------

    def now(self) -> str:
        return _iso(self.clock())

    def _journal(self, event: str, **data: object) -> None:
        self.store.journal(event, data)

    def _disposition(self, trigger_id: str, kind: str, detail: str = "") -> None:
        self.store.kv_set(f"disposition:{trigger_id}", {"kind": kind, "detail": detail, "at": self.now()})

    def disposition(self, trigger_id: str) -> dict | None:
        return self.store.kv_get(f"disposition:{trigger_id}")

    @property
    def paused(self) -> bool:
        return bool(self.store.kv_get("paused", False))

    def set_paused(self, paused: bool) -> None:
        self.store.kv_set("paused", paused)
        self._journal("processing.paused" if paused else "processing.resumed")

    def _thread(self, key: str, source: str = "") -> Thread:
        return self.store.get(Thread, key) or Thread(key=key, source=source or key.split(":", 1)[0])

    def tickets_on(self, thread: Thread) -> list[Ticket]:
        tickets = [self.store.get(Ticket, s.ticket_id) for s in thread.subscriptions]
        return [t for t in tickets if t is not None]

    def _add_input(self, ticket: Ticket, kind: str, text: str = "", trigger_id: str = "", owned: bool = False) -> None:
        if trigger_id and any(i.trigger_id == trigger_id and i.kind == kind for i in ticket.inputs):
            return
        ticket.inputs.append(Input(id=new_id("in"), kind=kind, text=text, trigger_id=trigger_id, owned=owned,
                                   at=self.now()))

    def _save(self, ticket: Ticket) -> None:
        ticket.updated_at = self.now()
        self.store.put(ticket)

    def _new_assignment(self, ticket: Ticket) -> Assignment:
        assignment = Assignment(id=new_id("as"), ticket_id=ticket.id, created_at=self.now())
        self.store.put(assignment)
        ticket.assignment_id = assignment.id
        ticket.assignment_ids.append(assignment.id)
        return assignment

    def _end_assignment(self, ticket: Ticket, reason: str) -> None:
        assignment = self.store.get(Assignment, ticket.assignment_id) if ticket.assignment_id else None
        if assignment and assignment.state != "ended":
            assignment.state = "ended"
            assignment.ended_at = self.now()
            assignment.end_reason = reason
            self.store.put(assignment)
            self.stopped_assignments.append((ticket.id, assignment.id))

    def _subscribe(self, thread: Thread, ticket_id: str, context_only: bool = False) -> None:
        existing = thread.subscription(ticket_id)
        if existing is None:
            thread.subscriptions.append(Subscription(ticket_id, context_only))
            self._journal("thread.subscribed", thread=thread.key, ticket=ticket_id, context_only=context_only)
        elif existing.context_only and not context_only:
            existing.context_only = False

    def _channel_for(self, ticket: Ticket, trigger_id: str = "") -> ChannelRef | None:
        if trigger_id:
            trigger = self.store.get(Trigger, trigger_id)
            if trigger and trigger.channels:
                return trigger.channels[0]
        return ticket.channels[0] if ticket.channels else None

    def _queue_message(self, channel: ChannelRef | None, text: str, kind: str, ticket_id: str = "") -> str:
        if channel is None or not text.strip():
            return ""
        message_id = new_id("msg")
        self.outbox.send(OutboundMessage(message_id, channel, text.strip(), kind, ticket_id))
        return message_id

    # -- policy ------------------------------------------------------------------------------------------------

    def set_policy(self, policy: Policy) -> None:
        """Install a new Policy. Running Evals are discarded and live Tickets it bears on are told why."""
        old = self.policy
        self.policy = policy
        if old.version == policy.version:
            return
        with self.store.atomic():
            for thread in self.store.list(Thread, tag="active"):
                if thread.eval_id:
                    self._discard_eval(thread, requeue=True)
            for ticket in self.store.list(Ticket, tag="open"):
                notice = self._policy_notice(old, policy, ticket.mandate)
                if notice:
                    self._add_input(ticket, "policy", notice)
                    self._save(ticket)
            self._journal("policy.changed", previous=old.version, current=policy.version)

    @staticmethod
    def _policy_notice(old: Policy, new: Policy, mandate: Mandate) -> str:
        lines = []
        for rule_id in mandate.rules:
            before, after = old.rule(rule_id), new.rule(rule_id)
            if after is None or not after.enabled:
                lines.append(f"Rule {rule_id!r} that this ticket was accepted under no longer applies.")
            elif before is None or before.instruction != after.instruction:
                lines.append(f"Rule {rule_id!r} changed. It now reads: {after.instruction}")
        source = new.sources.get(mandate.source)
        if mandate.basis == "allowlist" and (source is None or not source.is_allowed(mandate.requester)):
            lines.append(f"{mandate.requester} may no longer start work through {mandate.source}.")
        if mandate.strength == "inferred" and old.mode == "auto" and new.mode != "auto":
            lines.append("Proactive work is no longer accepted; finish only what a person can use.")
        if not lines:
            return ""
        return "The team's policy changed in a way that bears on this ticket:\n" + "\n".join(f"- {line}" for line in lines)

    # -- intake ------------------------------------------------------------------------------------------------

    def intake(self, trigger: Trigger) -> str:
        """Record a Trigger, fan it out to subscribed Tickets and queue it for Eval when nobody owns it."""
        with self.store.atomic():
            if self.store.get(Trigger, trigger.id) is not None:
                return "duplicate"
            self.store.put(trigger)
            owners: list[str] = []
            seen: set[str] = set()
            followup = False
            for key in trigger.threads:
                thread = self._thread(key, trigger.source)
                thread.trigger_ids.append(trigger.id)
                if trigger.title:
                    thread.title = trigger.title
                thread.updated_at = self.now()
                for sub in thread.subscriptions:
                    if sub.ticket_id in seen:
                        continue
                    seen.add(sub.ticket_id)
                    ticket = self.store.get(Ticket, sub.ticket_id)
                    if ticket is None or ticket.stopped:
                        continue
                    if not sub.context_only:
                        followup = True
                    if self._fan_out(ticket, trigger, owns=trigger.is_person and not sub.context_only):
                        owners.append(ticket.id)
                self.store.put(thread)
            if trigger.kind in ("pr_closed", "pr_reopened"):
                self._pull_request_state(trigger)
            if owners:
                self._disposition(trigger.id, "delivered", ",".join(owners))
                self._journal("trigger.delivered", trigger=trigger.id, tickets=owners)
                return "delivered"
            return self._queue_for_eval(trigger, followup)

    def _fan_out(self, ticket: Ticket, trigger: Trigger, owns: bool) -> bool:
        """Deliver a Trigger to one subscribed Ticket. Returns whether the Ticket now owns it."""
        if ticket.live:
            self._add_input(ticket, "trigger", trigger_id=trigger.id, owned=owns)
            if owns:
                ticket.human_since_report = True
                ticket.attention = ""
            self._save(ticket)
            return owns
        if owns:
            return self._resume(ticket, trigger)
        return False

    def _resume(self, ticket: Ticket, trigger: Trigger) -> bool:
        if not self.policy.revalidate(ticket.mandate, self.listens(ticket.origin_thread)):
            self._journal("ticket.resume_refused", ticket=ticket.id, trigger=trigger.id)
            return False
        ticket.state = "open"
        ticket.outcome = ""
        ticket.ended_by = ""
        ticket.attention = ""
        ticket.human_since_report = True
        self._add_input(ticket, "resume", RESUME_NOTE)
        self._add_input(ticket, "trigger", trigger_id=trigger.id, owned=True)
        self._end_assignment(ticket, "resumed")
        self._new_assignment(ticket)
        self._save(ticket)
        self._journal("ticket.resumed", ticket=ticket.id, trigger=trigger.id)
        return True

    def _queue_for_eval(self, trigger: Trigger, followup: bool, reason: str = "") -> str:
        gate = self.policy.gate(trigger, followup=followup)
        if not gate.admitted:
            self._disposition(trigger.id, "filtered", gate.reason)
            return "filtered"
        thread = self._thread(trigger.threads[0], trigger.source)
        if trigger.id not in thread.queued:
            thread.queued.append(trigger.id)
        if reason:
            thread.handbacks.setdefault(trigger.id, []).append(reason)
        self.store.put(thread)
        self._disposition(trigger.id, "queued", gate.strength or "")
        return "queued"

    # -- evals -------------------------------------------------------------------------------------------------

    def start_evals(self) -> list[Candidate]:
        """Start one Eval per Thread with queued Triggers and no Eval running. Returns the candidates to assess."""
        if self.paused:
            return []
        candidates = []
        with self.store.atomic():
            for thread in self.store.list(Thread, tag="active"):
                if thread.eval_id or not thread.queued:
                    continue
                candidate = self._start_eval(thread)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _start_eval(self, thread: Thread) -> Candidate | None:
        followup = any(
            not t.stopped and not getattr(thread.subscription(t.id), "context_only", True) for t in self.tickets_on(thread)
        )
        batch: list[Trigger] = []
        best = None
        for trigger_id in thread.queued:
            trigger = self.store.get(Trigger, trigger_id)
            if trigger is None:
                continue
            gate = self.policy.gate(trigger, followup=followup)
            if not gate.admitted:
                self._disposition(trigger.id, "filtered", gate.reason)
                continue
            batch.append(trigger)
            if best is None or (gate.strength == "explicit" and best[1].strength != "explicit"):
                best = (trigger, gate)
        thread.queued = []
        if not batch or best is None:
            self.store.put(thread)
            return None
        lead, gate = best
        now = self.clock()
        evaluation = Eval(
            id=new_id("ev"), thread_key=thread.key, trigger_ids=[t.id for t in batch], strength=gate.strength or "inferred",
            basis=gate.basis, cell=gate.cell, requester=lead.actor, source=lead.source,
            policy_version=self.policy.version, started_at=_iso(now),
            deadline=_iso(now + timedelta(seconds=self.policy.eval_timeout_seconds)),
        )
        thread.eval_id = evaluation.id
        self.store.put(evaluation)
        self.store.put(thread)
        for trigger in batch:
            self._disposition(trigger.id, "evaluating", evaluation.id)
        self._journal("eval.started", eval=evaluation.id, thread=thread.key, strength=evaluation.strength)
        return self.candidate(evaluation.id)

    def candidate(self, eval_id: str, context_limit: int = 20) -> Candidate | None:
        evaluation = self.store.get(Eval, eval_id)
        if evaluation is None:
            return None
        thread = self.store.get(Thread, evaluation.thread_key) or Thread(evaluation.thread_key, evaluation.source)
        batch = [t for t in (self.store.get(Trigger, i) for i in evaluation.trigger_ids) if t]
        earlier = [i for i in thread.trigger_ids if i not in evaluation.trigger_ids][-context_limit:]
        context = [t for t in (self.store.get(Trigger, i) for i in earlier) if t]
        handbacks = {k: v for k, v in thread.handbacks.items() if k in evaluation.trigger_ids}
        return Candidate(evaluation, thread, batch, context, self.tickets_on(thread), handbacks)

    def _discard_eval(self, thread: Thread, requeue: bool) -> None:
        evaluation = self.store.get(Eval, thread.eval_id)
        if evaluation and requeue:
            thread.queued = evaluation.trigger_ids + [t for t in thread.queued if t not in evaluation.trigger_ids]
        if evaluation:
            self.store.delete(Eval, evaluation.id)
        thread.eval_id = ""
        self.store.put(thread)

    def expire_evals(self) -> list[Decision]:
        """Treat Evals past their deadline as unavailable; they are retried twice before being recorded."""
        if self.paused:
            return []
        now = self.now()
        decisions = []
        for evaluation in self.store.list(Eval):
            if evaluation.deadline < now:
                decision = self.decide(evaluation.id, Outcome("unavailable", "the assessment timed out"))
                if decision:
                    decisions.append(decision)
        return decisions

    def decide(self, eval_id: str, outcome: Outcome) -> Decision | None:
        """Apply an assessment to its Eval. Returns the recorded Decision, or None when the Eval was requeued."""
        with self.store.atomic():
            evaluation = self.store.get(Eval, eval_id)
            if evaluation is None:
                return None
            thread = self._thread(evaluation.thread_key, evaluation.source)
            if evaluation.policy_version != self.policy.version:
                self._discard_eval(thread, requeue=True)
                self._journal("eval.discarded", eval=eval_id, reason="policy changed")
                return None
            if outcome.kind == "unavailable":
                if thread.eval_retries < MAX_EVAL_RETRIES:
                    thread.eval_retries += 1
                    self._discard_eval(thread, requeue=True)
                    self._journal("eval.requeued", eval=eval_id, reason=outcome.reason)
                    return None
            decision = self._apply(evaluation, thread, outcome)
            thread.eval_id = ""
            thread.eval_retries = 0
            thread.decision_ids.append(decision.id)
            for trigger_id in evaluation.trigger_ids:
                thread.handbacks.pop(trigger_id, None)
                self._disposition(trigger_id, "decided", decision.id)
            self.store.put(decision)
            self.store.put(thread)
            self.store.delete(Eval, evaluation.id)
            self._journal("decision.recorded", decision=decision.id, kind=decision.kind, thread=thread.key,
                          ticket=decision.ticket_id)
            return decision

    def _decision(self, evaluation: Eval, kind: str, reason: str, **extra: object) -> Decision:
        return Decision(id=new_id("dc"), eval_id=evaluation.id, thread_key=evaluation.thread_key, kind=kind,  # type: ignore[arg-type]
                        reason=reason, trigger_ids=list(evaluation.trigger_ids), strength=evaluation.strength,
                        policy_version=evaluation.policy_version, at=self.now(), **extra)  # type: ignore[arg-type]

    def _apply(self, evaluation: Eval, thread: Thread, outcome: Outcome) -> Decision:
        kind = outcome.kind
        if kind == "unavailable":
            return self._decision(evaluation, "unavailable", outcome.reason)
        if kind == "accept":
            return self._accept(evaluation, thread, outcome)
        if kind == "attach":
            return self._attach(evaluation, thread, outcome)
        if kind == "stop":
            return self._stop_decision(evaluation, thread, outcome)
        return self._decision(evaluation, "ignore", outcome.reason or "nothing to do",
                              rules=[r for r in outcome.rules if self.policy.rule(r)])

    def _accept(self, evaluation: Eval, thread: Thread, outcome: Outcome) -> Decision:
        rules = tuple(r for r in outcome.rules if (rule := self.policy.rule(r)) and rule.enabled)
        if evaluation.strength == "inferred":
            if not rules:
                return self._decision(evaluation, "ignore", "no enabled rule matched: " + outcome.reason)
            if self.policy.mode != "auto":
                return self._decision(evaluation, "ignore", outcome.reason, rules=list(rules), would_accept=True)
            if not self.policy.admits_inferred(rules):
                return self._decision(evaluation, "ignore", "the policy does not admit these rules", rules=list(rules))
        triggers = [t for t in (self.store.get(Trigger, i) for i in evaluation.trigger_ids) if t]
        lead = next((t for t in reversed(triggers) if t.actor == evaluation.requester), triggers[-1])
        taken = {t.name for t in self.store.list(Ticket)}
        first_line = lead.text.strip().splitlines()[0] if lead.text.strip() else ""
        title = outcome.title.strip() or lead.title or first_line or "Work"
        mandate = Mandate(policy_version=evaluation.policy_version, strength=evaluation.strength, basis=evaluation.basis,
                          scope=outcome.scope.strip() or title, requester=evaluation.requester,
                          source=evaluation.source, rules=rules, cell=evaluation.cell)
        decision = self._decision(evaluation, "accept", outcome.reason, rules=list(rules))
        channels: list[ChannelRef] = []
        for trigger in triggers:
            for channel in trigger.channels:
                if channel not in channels:
                    channels.append(channel)
        ticket = Ticket(
            id=new_id("tk"), name=unique_name(slug(outcome.name or title), taken), title=title[:200], mandate=mandate,
            decision_id=decision.id, origin_thread=thread.key, origin_trigger=lead.id, channels=channels,
            created_at=self.now(),
        )
        for trigger in triggers:
            self._add_input(ticket, "trigger", trigger_id=trigger.id, owned=trigger.is_person)
        if outcome.note.strip():
            self._add_input(ticket, "note", outcome.note.strip()[:1000])
        self._new_assignment(ticket)
        self._subscribe(thread, ticket.id)
        for key in lead.threads[1:]:
            other = self._thread(key, lead.source)
            self._subscribe(other, ticket.id)
            self.store.put(other)
        self._save(ticket)
        decision = replace(decision, ticket_id=ticket.id)
        if outcome.acknowledgement:
            self._queue_message(self._channel_for(ticket, lead.id), outcome.acknowledgement, "acknowledgement",
                                ticket.id)
        self._journal("ticket.created", ticket=ticket.id, name=ticket.name, strength=mandate.strength)
        return decision

    def _attach(self, evaluation: Eval, thread: Thread, outcome: Outcome) -> Decision:
        ticket = self.store.get(Ticket, outcome.ticket_id)
        if evaluation.strength != "explicit":
            return self._decision(evaluation, "ignore", "attach refused: only an explicit request can attach")
        if ticket is None or ticket.stopped:
            return self._decision(evaluation, "ignore", "attach refused: no such ticket, or a person stopped it")
        triggers = [t for t in (self.store.get(Trigger, i) for i in evaluation.trigger_ids) if t]
        if not ticket.live and not self.policy.revalidate(ticket.mandate, self.listens(ticket.origin_thread)):
            return self._decision(evaluation, "ignore", "attach refused: the ticket's mandate no longer holds")
        self._subscribe(thread, ticket.id)
        for trigger in triggers:
            if ticket.live:
                self._add_input(ticket, "trigger", trigger_id=trigger.id, owned=trigger.is_person)
            else:
                self._resume(ticket, trigger)
        if outcome.note.strip():
            self._add_input(ticket, "note", outcome.note.strip()[:1000])
        ticket.human_since_report = True
        self._save(ticket)
        if outcome.acknowledgement and triggers:
            self._queue_message(self._channel_for(ticket, triggers[-1].id), outcome.acknowledgement,
                                "acknowledgement", ticket.id)
        return self._decision(evaluation, "attach", outcome.reason, ticket_id=ticket.id)

    def _stop_decision(self, evaluation: Eval, thread: Thread, outcome: Outcome) -> Decision:
        if evaluation.strength != "explicit":
            return self._decision(evaluation, "ignore", "stop refused: only an explicit request can stop work")
        ticket = self.store.get(Ticket, outcome.ticket_id)
        on_thread = {t.id for t in self.tickets_on(thread)}
        if ticket is None or ticket.id not in on_thread or ticket.stopped:
            return self._decision(evaluation, "ignore", "stop refused: no such ticket on this thread")
        self._end(ticket, "stopped", "person", outcome.reason)
        if outcome.acknowledgement:
            self._queue_message(self._channel_for(ticket, evaluation.trigger_ids[-1]), outcome.acknowledgement,
                                "acknowledgement", ticket.id)
        return self._decision(evaluation, "stop", outcome.reason, ticket_id=ticket.id)

    # -- ticket lifecycle --------------------------------------------------------------------------------------

    def _end(self, ticket: Ticket, outcome: str, by: str, reason: str = "") -> None:
        ticket.state = "ended"
        ticket.outcome = outcome
        ticket.ended_by = by
        ticket.attention = ""
        self._end_assignment(ticket, reason or outcome)
        released = [i for i in ticket.inputs if i.owned and not i.read and i.trigger_id]
        for item in released:
            item.owned = False
        self._save(ticket)
        self._journal("ticket.ended", ticket=ticket.id, outcome=outcome, by=by, reason=reason)
        if outcome == "stopped":
            return
        for item in released:
            self._release(item.trigger_id, f"ticket {ticket.name} ended before reading it")

    def _owned_elsewhere(self, trigger_id: str) -> bool:
        return any(i.trigger_id == trigger_id and i.owned for t in self.store.list(Ticket, tag="open") for i in t.inputs)

    def _release(self, trigger_id: str, reason: str) -> None:
        trigger = self.store.get(Trigger, trigger_id)
        if trigger is None or self._owned_elsewhere(trigger_id):
            return
        thread = self._thread(trigger.threads[0], trigger.source)
        followup = any(not t.stopped for t in self.tickets_on(thread))
        self._queue_for_eval(trigger, followup, reason)

    def stop(self, ticket_id: str, reason: str = "stopped from the panel", by: str = "person") -> bool:
        """A person's stop command: ends the Ticket directly and withdraws its Mandate."""
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            if ticket is None or ticket.stopped:
                return False
            self._end(ticket, "stopped", by, reason)
            return True

    def retry(self, ticket_id: str) -> bool:
        """Start a new Assignment for a Ticket that waits on a failed delivery or a blocked or failed Report."""
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            if ticket is None or not ticket.live:
                return False
            if not (ticket.delivery_failures or ticket.held):
                return False
            self._end_assignment(ticket, "retry")
            for item in ticket.inputs:
                if not item.read:
                    item.delivered = False
            ticket.delivery_failures = 0
            ticket.human_since_report = True
            self._new_assignment(ticket)
            self._save(ticket)
            self._journal("ticket.retried", ticket=ticket.id)
            return True

    def reevaluate(self, trigger_id: str) -> bool:
        """Send a Trigger to Eval again, such as a follow-up a person wants judged as new work."""
        with self.store.atomic():
            trigger = self.store.get(Trigger, trigger_id)
            if trigger is None:
                return False
            for ticket in self.store.list(Ticket, tag="open"):
                changed = False
                for item in ticket.inputs:
                    if item.trigger_id == trigger_id and item.owned:
                        item.owned = False
                        changed = True
                if changed:
                    self._save(ticket)
            thread = self._thread(trigger.threads[0], trigger.source)
            followup = any(not t.stopped for t in self.tickets_on(thread))
            return self._queue_for_eval(trigger, followup, "a person asked to evaluate it again") == "queued"

    # -- executor side -----------------------------------------------------------------------------------------

    def pending_launches(self) -> list[tuple[Ticket, Assignment]]:
        if self.paused:
            return []
        launches = []
        for ticket in self.store.list(Ticket, tag="open"):
            assignment = self.store.get(Assignment, ticket.assignment_id)
            if assignment and assignment.state == "pending" and not ticket.held:
                launches.append((ticket, assignment))
        return launches

    def deliverable(self) -> list[Ticket]:
        """Open Tickets whose running Assignment has Inputs waiting and may take them now."""
        if self.paused:
            return []
        result = []
        for ticket in self.store.list(Ticket, tag="open"):
            if ticket.held or not ticket.undelivered():
                continue
            assignment = self.store.get(Assignment, ticket.assignment_id)
            if assignment and assignment.state == "running":
                result.append(ticket)
        return result

    def _live_assignment(self, ticket_id: str, assignment_id: str) -> Ticket | None:
        ticket = self.store.get(Ticket, ticket_id)
        if ticket is None or not ticket.live or ticket.assignment_id != assignment_id:
            return None
        return ticket

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        with self.store.atomic():
            assignment = self.store.get(Assignment, assignment_id)
            if assignment is None or assignment.state != "pending":
                return
            assignment.state = "running"
            assignment.started_at = self.now()
            self.store.put(assignment)
            self._journal("assignment.started", ticket=ticket_id, assignment=assignment_id)

    def inputs_delivered(self, ticket_id: str, input_ids: list[str]) -> None:
        self._mark_inputs(ticket_id, input_ids, delivered=True)

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        self._mark_inputs(ticket_id, input_ids, delivered=True, read=True)

    def _mark_inputs(self, ticket_id: str, input_ids: list[str], delivered: bool, read: bool = False) -> None:
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            if ticket is None:
                return
            wanted = set(input_ids)
            for item in ticket.inputs:
                if item.id in wanted:
                    item.delivered = item.delivered or delivered
                    item.read = item.read or read
            ticket.delivery_failures = 0
            self._save(ticket)

    def delivery_failed(self, ticket_id: str, error: str) -> None:
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            if ticket is None or not ticket.live:
                return
            ticket.delivery_failures += 1
            self._save(ticket)
            self._journal("delivery.failed", ticket=ticket_id, error=error[:300])

    def report(self, ticket_id: str, assignment_id: str, report: Report) -> None:
        """Apply a worker's Report. Only the current Assignment of an open Ticket can report."""
        with self.store.atomic():
            ticket = self._live_assignment(ticket_id, assignment_id)
            if ticket is None:
                return
            report = replace(report, at=report.at or self.now())
            ticket.reports.append(report)
            ticket.human_since_report = False
            if report.question.strip():
                ticket.attention = report.question.strip()
                self._queue_message(self._channel_for(ticket, report.question_trigger), ticket.attention, "question",
                                    ticket.id)
            if report.pull_request.strip():
                self._follow_pull_request(ticket, report.pull_request.strip())
            self._journal("ticket.reported", ticket=ticket.id, status=report.status, summary=report.summary[:300])
            if report.status == "dropped":
                self._end(ticket, "dropped", "executor", report.summary[:300])
                return
            if report.status == "ready" and not self._open_pull_requests(ticket):
                self._end(ticket, "completed", "executor", report.summary[:300])
                return
            self._save(ticket)

    def _open_pull_requests(self, ticket: Ticket) -> list[str]:
        relevant = ticket.opened_pull_requests or ticket.pull_requests
        return [k for k in relevant if k not in ticket.closed_pull_requests]

    def _follow_pull_request(self, ticket: Ticket, ref: str) -> None:
        key = self.resolve_thread(ref)
        if not key:
            return
        if key not in ticket.pull_requests:
            ticket.pull_requests.append(key)
        if key != ticket.origin_thread and key not in ticket.opened_pull_requests:
            ticket.opened_pull_requests.append(key)
        ticket.closed_pull_requests.pop(key, None)
        thread = self._thread(key)
        self._subscribe(thread, ticket.id)
        self.store.put(thread)

    def _pull_request_state(self, trigger: Trigger) -> None:
        key = trigger.threads[0]
        thread = self.store.get(Thread, key)
        if thread is None:
            return
        for ticket in self.tickets_on(thread):
            if key not in ticket.pull_requests:
                continue
            if trigger.kind == "pr_reopened":
                ticket.closed_pull_requests.pop(key, None)
                self._save(ticket)
                continue
            result = "merged" if trigger.meta.get("merged") else "closed"
            ticket.closed_pull_requests[key] = result
            last = ticket.last_report
            busy = last is not None and last.status == "progress"
            if ticket.live and not self._open_pull_requests(ticket) and not busy:
                self._end(ticket, result, "source", f"pull request {result}")
            else:
                self._save(ticket)

    def reply(self, ticket_id: str, text: str, purpose: str, trigger_id: str = "") -> str:
        """Queue a worker's message to its Ticket's conversation. Returns an error text, or empty on success."""
        if purpose not in REPLY_PURPOSES:
            return f"unknown purpose {purpose!r}; use one of {', '.join(REPLY_PURPOSES)}"
        words = len(text.split())
        if words == 0:
            return "the message is empty"
        if words > MAX_REPLY_WORDS:
            return f"the message has {words} words; the limit is {MAX_REPLY_WORDS}. Shorten it and keep details in the PR."
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            if ticket is None or not ticket.live:
                return "the ticket is not open"
            channel = self._channel_for(ticket, trigger_id)
            if channel is None:
                return "the ticket has no conversation to reply to"
            self._queue_message(channel, text, purpose, ticket.id)
            for item in ticket.inputs:
                if trigger_id and item.trigger_id == trigger_id:
                    item.read = True
            self._save(ticket)
        return ""

    def hand_back(self, ticket_id: str, trigger_id: str, reason: str) -> str:
        """A worker returns a person's message that asks for something beyond its Ticket. Returns an error text."""
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            if ticket is None:
                return "unknown ticket"
            item = next((i for i in ticket.inputs if i.trigger_id == trigger_id and i.owned), None)
            if item is None:
                return "this ticket does not own that message; nothing to hand back"
            item.owned = False
            item.read = True
            self._save(ticket)
            self._journal("trigger.handed_back", ticket=ticket_id, trigger=trigger_id, reason=reason[:300])
            if not self._owned_elsewhere(trigger_id):
                trigger = self.store.get(Trigger, trigger_id)
                if trigger:
                    thread = self._thread(trigger.threads[0], trigger.source)
                    followup = any(not t.stopped for t in self.tickets_on(thread))
                    self._queue_for_eval(trigger, followup, reason[:300])
        return ""

    def follow(self, ticket_id: str, ref: str) -> str:
        """Subscribe a Ticket to another Thread for context only. Returns the Thread key, or empty if unknown."""
        key = self.resolve_thread(ref) or ""
        if not key:
            return ""
        with self.store.atomic():
            thread = self._thread(key)
            if thread.subscription(ticket_id) is None:
                self._subscribe(thread, ticket_id, context_only=True)
                self.store.put(thread)
        return key

    def unsubscribe(self, ticket_id: str, thread_key: str, by: str = "executor") -> str:
        with self.store.atomic():
            ticket = self.store.get(Ticket, ticket_id)
            thread = self.store.get(Thread, thread_key)
            if ticket is None or thread is None or thread.subscription(ticket_id) is None:
                return "the ticket does not follow that thread"
            if by == "executor" and (thread_key == ticket.origin_thread or thread_key in ticket.pull_requests):
                return "a ticket cannot drop its own origin thread or a pull request it follows"
            thread.subscriptions = [s for s in thread.subscriptions if s.ticket_id != ticket_id]
            if thread_key in ticket.pull_requests:
                ticket.pull_requests.remove(thread_key)
                self._save(ticket)
            self.store.put(thread)
            self._journal("thread.unsubscribed", thread=thread_key, ticket=ticket_id, by=by)
        return ""

    # -- recovery ----------------------------------------------------------------------------------------------

    def recover(self) -> None:
        """After a restart: requeue unfinished Evals and restart Assignments that were busy."""
        with self.store.atomic():
            for thread in self.store.list(Thread, tag="active"):
                if thread.eval_id:
                    self._discard_eval(thread, requeue=True)
            for ticket in self.store.list(Ticket, tag="open"):
                for item in ticket.inputs:
                    if item.delivered and not item.read:
                        item.delivered = False
                assignment = self.store.get(Assignment, ticket.assignment_id)
                if assignment and assignment.state == "running":
                    last = ticket.last_report
                    busy = last is None or last.status == "progress"
                    if busy:
                        self._end_assignment(ticket, "host restarted")
                        self._add_input(ticket, "recovery", RECOVERY_NOTE)
                        self._new_assignment(ticket)
                self._save(ticket)
            self.stopped_assignments.clear()
            self._journal("runtime.recovered")

    def take_stopped_assignments(self) -> list[tuple[str, str]]:
        """Assignments ended since the last call, for the runtime to stop in the Executor."""
        stopped, self.stopped_assignments = self.stopped_assignments, []
        return stopped
