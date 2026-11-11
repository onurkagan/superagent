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

from superagent.agents.assessor import Assessor
from superagent.agents.executor import Executor
from superagent.agents.prompts import PromptLibrary
from superagent.agents.worker import WorkerSpec
from superagent.cards import assignment_card, input_card
from superagent.config import Settings
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
    sources: list[Source] = []
    if settings.github and settings.github.enabled:
        gh = settings.github
        sources.append(GitHubSource(GitHubConfig(repository=gh.repository, account=gh.account,
                                                 receive_all=dict(gh.receive_all), poll_seconds=gh.poll_seconds,
                                                 disclosure=gh.disclosure), store, prompts=prompts))
    if settings.inbox and settings.inbox.enabled:
        sources.append(InboxSource(store, settings.inbox_path, settings.replies_path, settings.inbox.poll_seconds))
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
        self.client = client or AnthropicModelClient(base_url=settings.model.base_url,
                                                     fallbacks=settings.model.fallbacks)
        self.registry = SourceRegistry(sources if sources is not None else build_sources(settings, self.store,
                                                                                         self.prompts))
        self.outbox = Outbox(self.store)
        self.engine = Engine(self.store, settings.build_policy(), self.outbox, listens=self.registry.listens,
                             resolve_thread=self.registry.resolve_thread)
        self.events: queue.Queue = queue.Queue()
        self.executor = Executor(self.client, self.events, root=settings.work_root, prepare=settings.execution.prepare,
                                 concurrency=settings.execution.concurrency,
                                 resolve_thread=self.registry.resolve_thread)
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
            if kind == "stop":
                self.engine.stop(args["ticket"], args.get("reason") or "stopped from the panel")
            elif kind == "retry":
                self.engine.retry(args["ticket"])
            elif kind == "pause":
                self.engine.set_paused(True)
            elif kind == "resume":
                self.engine.set_paused(False)
            elif kind == "reevaluate":
                self.engine.reevaluate(args["trigger"])
            else:
                log.warning("unknown command %s", kind)

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
            engine.hand_back(args[0], args[1], args[2])
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
        system = self.prompts.worker_system(variables, [s.instructions() for s in self.registry], rules)
        return WorkerSpec(
            ticket_id=ticket.id, assignment_id=assignment.id, name=ticket.name, directory=directory,
            session_path=settings.sessions_dir / f"{ticket.id}.json", system=system, model=settings.model.worker,
            effort=settings.model.worker_effort, env={v.key: v.value for v in variables}, blocked_commands=blocked,
            max_steps=settings.execution.max_steps,
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
