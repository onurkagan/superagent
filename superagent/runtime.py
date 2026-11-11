"""The runtime: wires Sources, the Engine, the assessor, the Executor and the outbox into one loop.

Every tick it applies commands from the panel and CLI, applies events from workers and assessments, polls the Sources
that are due, starts Evals, launches and wakes workers, stops ended Assignments and flushes the outbox. Only this
thread calls the Engine. See docs/007-architecture.md.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from superagent.agents.assessor import Assessor, to_outcome
from superagent.agents.claude_code import ClaudeCodeCLI, ClaudeCodeModelClient, claude_code_turn
from superagent.agents.executor import Executor
from superagent.agents.prompts import PromptLibrary
from superagent.agents.worker import WorkerSpec, run_turn
from superagent.cards import assignment_card, input_card
from superagent.commands import ERROR_PREFIX, respond
from superagent.config import GitHubSettings, InboxSettings, Settings
from superagent.core.engine import Candidate, Engine, Outcome
from superagent.core.model import Assignment, Ticket, Trigger
from superagent.llm.client import AnthropicModelClient, ModelClient
from superagent.outbox import Outbox, Sender
from superagent.sources.base import Source, SourceRegistry, Variable
from superagent.sources.github import GitHubConfig, GitHubSource
from superagent.sources.inbox import InboxSource
from superagent.store import Store

log = logging.getLogger(__name__)


def build_sources(settings: Settings, store: Store, prompts: PromptLibrary) -> list[Source]:
    """One Source per enabled ``[sources.<name>]`` table; a GitHub Source may watch many repositories."""
    sources: list[Source] = []
    for name, config in settings.sources().items():
        if isinstance(config, GitHubSettings):
            sources.append(GitHubSource(
                GitHubConfig(repositories=config.repositories, account=config.account,
                             receive_all=dict(config.receive_all), poll_seconds=config.poll_seconds,
                             disclosure=config.disclosure),
                store, prompts=prompts, name=name))
        elif isinstance(config, InboxSettings):
            path, replies = settings.inbox_files(config)
            source = InboxSource(store, path, replies, config.poll_seconds)
            source.name = name
            sources.append(source)
    return sources


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        store: Store | None = None,
        client: ModelClient | None = None,
        sources: list[Source] | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.prompts = PromptLibrary(settings.prompts_dir)
        worker_client: Any
        if settings.model.backend == "claude-code":
            cli = ClaudeCodeCLI(settings.model.claude_command)
            self.client = client or ClaudeCodeModelClient(cli)
            worker_client, turn = cli, claude_code_turn
        else:
            self.client = client or AnthropicModelClient(base_url=settings.model.base_url,
                                                         fallbacks=settings.model.fallbacks)
            worker_client, turn = self.client, run_turn
        self.registry = SourceRegistry(sources if sources is not None else build_sources(settings, self.store,
                                                                                         self.prompts))
        self.outbox = Outbox(self.store)
        self.engine = Engine(self.store, settings.build_policy(), self.outbox, listens=self.registry.listens,
                             resolve_thread=self.registry.resolve_thread)
        self.events: queue.Queue = queue.Queue()
        self.executor = Executor(worker_client, self.events, root=settings.work_root,
                                 prepare=settings.execution.prepare, concurrency=settings.execution.concurrency,
                                 resolve_thread=self.registry.resolve_thread, run=turn)
        self.assessor = Assessor(self.client, self.prompts, settings.model.assessor, settings.model.assessor_effort)
        self.clock = clock
        self._assessments = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assessor")
        self._next_poll: dict[str, float] = {}
        self._started = False

    # -- lifecycle ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        problems = self.prompts.validate()
        if problems:
            raise ValueError("invalid prompt overrides:\n" + "\n".join(problems))
        self.engine.recover()
        self._started = True

    def run_forever(self, stop: threading.Event, interval: float = 1.0) -> None:
        if not self._started:
            self.start()
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the agent
                log.exception("tick failed")
            stop.wait(interval)
        self.shutdown()

    def shutdown(self) -> None:
        self.executor.shutdown()
        self._assessments.shutdown(wait=False, cancel_futures=True)

    # -- the loop ----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        self._apply_commands()
        self._apply_events()
        self._poll_sources()
        for candidate in self.engine.start_evals():
            self._assess(candidate)
        self.engine.expire_evals()
        self._apply_events()
        self._launch()
        self._deliver()
        self._stop_ended()
        if not self.engine.paused:
            self.outbox.flush(self._sender_for, self.registry.listens_channel)

    def _apply_commands(self) -> None:
        for kind, args in self.store.take_commands():
            try:
                outcome = self._apply_command(kind, args)
            except Exception as exc:  # noqa: BLE001 - a bad command must not stop the loop
                log.exception("command %s failed", kind)
                outcome = f"{ERROR_PREFIX}{exc}"
            respond(self.store, args, outcome)

    def _apply_command(self, kind: str, args: dict[str, Any]) -> str:
        """Apply one command and describe its outcome; refusals start with ``error:``."""
        engine = self.engine
        if kind == "stop":
            stopped = engine.stop(args["ticket"], args.get("reason") or "stopped from the panel", by=args.get("by") or "person")
            return "stopped" if stopped else f"{ERROR_PREFIX}the ticket is unknown or already stopped"
        if kind == "retry":
            if engine.retry(args["ticket"]):
                return "a new assignment starts"
            return f"{ERROR_PREFIX}only an open ticket that waits after a blocked or failed report, or a failed delivery, " \
                   "can be retried"
        if kind in ("pause", "resume"):
            engine.set_paused(kind == "pause")
            return "processing paused" if kind == "pause" else "processing resumed"
        if kind == "reevaluate":
            if engine.reevaluate(args["trigger"]):
                return "queued for evaluation"
            return f"{ERROR_PREFIX}unknown message, or the gate does not admit it"
        if kind == "note":
            error = engine.note(args["ticket"], str(args.get("text") or ""), str(args.get("note_kind") or "orchestrator"))
            return f"{ERROR_PREFIX}{error}" if error else "noted"
        if kind == "notify":
            error = engine.reply(args["ticket"], str(args.get("text") or ""), "answer")
            return f"{ERROR_PREFIX}{error}" if error else "queued for delivery"
        if kind == "decide":
            decision = engine.decide(args["eval"], to_outcome(args.get("outcome") or {}))
            if decision is None:
                return f"{ERROR_PREFIX}no such evaluation: it was already decided, expired, or restarted after a " \
                       "policy change"
            ticket = self.store.get(Ticket, decision.ticket_id) if decision.ticket_id else None
            named = f" ticket {ticket.name} ({ticket.id})" if ticket else ""
            return f"{decision.kind}{named}: {decision.reason}"
        log.warning("unknown command %s", kind)
        return f"{ERROR_PREFIX}unknown command {kind!r}"

    def _apply_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            kind, *args = event
            try:
                self._apply_event(kind, args)
            except Exception:  # noqa: BLE001
                log.exception("could not apply event %s", kind)

    def _apply_event(self, kind: str, args: list[Any]) -> None:
        engine = self.engine
        if kind == "assessed":
            engine.decide(args[0], args[1])
        elif kind == "started":
            engine.assignment_started(args[0], args[1])
        elif kind == "delivered":
            engine.inputs_delivered(args[0], args[1])
        elif kind == "read":
            engine.inputs_read(args[0], args[1])
        elif kind == "report":
            engine.report(args[0], args[1], args[2])
        elif kind == "reply":
            error = engine.reply(args[0], args[1], args[2], args[3])
            if error:
                log.warning("reply of %s refused: %s", args[0], error)
        elif kind == "hand_back":
            error = engine.hand_back(args[0], args[1], args[2])
            if error:
                log.warning("hand-back of %s by %s refused: %s", args[1], args[0], error)
        elif kind == "follow":
            engine.follow(args[0], args[1])
        else:
            log.warning("unknown event %s", kind)

    def _poll_sources(self) -> None:
        now = self.clock()
        listened: set[str] | None = None
        for source in self.registry:
            if now < self._next_poll.get(source.name, 0.0):
                continue
            self._next_poll[source.name] = now + source.poll_seconds
            if listened is None:
                listened = self.engine.listened_threads()
            try:
                triggers = source.poll({k for k in listened if k.startswith(source.name + ":")})
            except Exception:  # noqa: BLE001 - a broken Source must not stop the others
                log.exception("polling %s failed", source.name)
                continue
            for trigger in triggers:
                self.engine.intake(trigger)
            source.acknowledge()

    def _assess(self, candidate: Candidate) -> None:
        policy = self.engine.policy

        def run() -> None:
            try:
                outcome = self.assessor.assess(candidate, policy)
            except Exception as exc:  # noqa: BLE001
                outcome = Outcome("unavailable", f"assessment crashed: {exc}")
            self.events.put(("assessed", candidate.eval.id, outcome))

        self._assessments.submit(run)

    # -- execution ---------------------------------------------------------------------------------------------

    def worker_spec(self, ticket: Ticket, assignment: Assignment) -> WorkerSpec:
        settings = self.settings
        directory = settings.work_root / ticket.name
        blocked = settings.execution.blocked_commands
        origin = self.store.get(Trigger, ticket.origin_trigger)
        variables = [
            Variable("SUPERAGENT_DIR", str(directory), "The ticket directory, where you work"),
            Variable("SUPERAGENT_NAME", ticket.name, "The ticket's short name; use it as the branch name"),
            Variable("SUPERAGENT_TICKET", ticket.id, "The ticket id"),
            Variable("SUPERAGENT_BLOCKED_COMMANDS", ",".join(blocked), "Commands you may not run"),
        ]
        variables += self.registry.variables(origin)
        policy = self.engine.policy
        rules = [r for r in (policy.rule(i) for i in ticket.mandate.rules) if r] \
            if ticket.mandate.strength == "inferred" else []
        system = self.prompts.worker_system(variables, self.registry.instructions(), rules)
        return WorkerSpec(
            ticket_id=ticket.id, assignment_id=assignment.id, name=ticket.name, directory=directory,
            session_path=settings.sessions_dir / f"{ticket.id}.json", system=system, model=settings.model.worker,
            effort=settings.model.worker_effort, env={v.key: v.value for v in variables}, blocked_commands=blocked,
            max_steps=settings.execution.max_steps, turn_timeout_seconds=settings.execution.turn_timeout_seconds,
        )

    def _cards(self, ticket: Ticket) -> list[tuple[str, str]]:
        cards = []
        for item in ticket.undelivered():
            trigger = self.store.get(Trigger, item.trigger_id) if item.trigger_id else None
            cards.append((item.id, input_card(item, trigger)))
        return cards

    def _launch(self) -> None:
        for ticket, assignment in self.engine.pending_launches():
            try:
                spec = self.worker_spec(ticket, assignment)
            except Exception as exc:  # noqa: BLE001 - such as gh failing while reading variables
                log.warning("cannot launch %s yet: %s", ticket.id, exc)
                continue
            card = assignment_card(ticket, assignment, self.engine.policy)
            self.executor.start(spec, [card, *self._cards(ticket)])

    def _deliver(self) -> None:
        for ticket in self.engine.deliverable():
            assignment = self.store.get(Assignment, ticket.assignment_id)
            if assignment is None:
                continue
            person = any(i.owned for i in ticket.undelivered())
            try:
                self.executor.deliver(self.worker_spec(ticket, assignment), self._cards(ticket), person=person)
            except Exception as exc:  # noqa: BLE001
                self.engine.delivery_failed(ticket.id, str(exc))

    def _stop_ended(self) -> None:
        for ticket_id, assignment_id in self.engine.take_stopped_assignments():
            self.executor.stop(ticket_id, assignment_id)

    def _sender_for(self, source_name: str) -> Sender | None:
        return self.registry.get(source_name)

    # -- read side ---------------------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.engine.paused,
            "policy": self.engine.policy.version,
            "executor": self.executor.status(),
            "sources": {s.name: vars(s.health()) for s in self.registry},
            "outbox_problems": len(self.outbox.problems()),
        }
