"""The Executor: runs worker turns in threads, one directory and one session per Ticket.

A Ticket holds a worker slot while it prepares its directory and while a turn runs. The concurrency limit applies to
those slots, plus one extra slot that only a person's message can use, so new work never keeps a person from
continuing an existing Ticket. Workers never touch the store: everything they cause goes back to the runtime as an
event on a queue and is applied by the Engine on the main thread. See docs/003-execution.md.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from superagent.agents.worker import WorkerSpec, run_turn
from superagent.core.engine import MAX_REPLY_WORDS, REPLY_PURPOSES
from superagent.core.model import Report
from superagent.llm.client import ModelClient

log = logging.getLogger(__name__)

PREPARE_TIMEOUT_SECONDS = 600


@dataclass
class _Wake:
    spec: WorkerSpec
    inputs: list[tuple[str, str]]
    launch: bool
    person: bool


@dataclass
class _State:
    assignment_id: str
    inbox: deque[tuple[str, str]] = field(default_factory=deque)
    accepted: set[str] = field(default_factory=set)
    busy: bool = False
    queued: _Wake | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


class _Host:
    """The worker's way to reach people and the domain; validated here, applied by the Engine."""

    def __init__(self, executor: Executor, spec: WorkerSpec) -> None:
        self.executor = executor
        self.spec = spec

    def reply(self, text: str, purpose: str, trigger_id: str) -> str:
        if purpose not in REPLY_PURPOSES:
            return f"unknown purpose {purpose!r}"
        words = len(text.split())
        if words == 0:
            return "the message is empty"
        if words > MAX_REPLY_WORDS:
            return f"the message has {words} words; the limit is {MAX_REPLY_WORDS}. Keep details in the pull request."
        self.executor.emit("reply", self.spec.ticket_id, text, purpose, trigger_id)
        return ""

    def hand_back(self, trigger_id: str, reason: str) -> str:
        self.executor.emit("hand_back", self.spec.ticket_id, trigger_id, reason or "beyond this ticket")
        return ""

    def follow(self, ref: str) -> str:
        key = self.executor.resolve_thread(ref) or ""
        if key:
            self.executor.emit("follow", self.spec.ticket_id, ref)
        return key


class _Events:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        self.executor.emit("read", ticket_id, list(input_ids))

    def progress(self, ticket_id: str, assignment_id: str, report: Report) -> None:
        self.executor.emit("report", ticket_id, assignment_id, report)


class Executor:
    def __init__(
        self,
        client: ModelClient,
        events: queue.Queue,
        *,
        root: Path,
        prepare: str = "",
        concurrency: int = 2,
        resolve_thread: Callable[[str], str | None] = lambda ref: None,
        run: Callable[..., Report | None] = run_turn,
        prepare_timeout: int = PREPARE_TIMEOUT_SECONDS,
    ) -> None:
        self.client = client
        self.events = events
        self.root = Path(root)
        self.prepare = prepare
        self.concurrency = max(1, concurrency)
        self.resolve_thread = resolve_thread
        self.run = run
        self.prepare_timeout = prepare_timeout
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._waiting: deque[_Wake] = deque()
        self._running = 0
        self._threads: list[threading.Thread] = []

    def emit(self, kind: str, *args: object) -> None:
        self.events.put((kind, *args))

    # -- commands from the runtime -----------------------------------------------------------------------------

    def start(self, spec: WorkerSpec, inputs: list[tuple[str, str]]) -> bool:
        """Queue the launch of a pending Assignment. Returns False when it was already launched."""
        with self._lock:
            state = self._states.get(spec.ticket_id)
            if state is not None and state.assignment_id == spec.assignment_id:
                return False
            if state is not None:
                state.cancel.set()
            state = _State(spec.assignment_id)
            self._states[spec.ticket_id] = state
            fresh = self._accept(state, inputs)
            wake = _Wake(spec, fresh, launch=True, person=False)
            state.queued = wake
            self._waiting.append(wake)
        self._delivered(spec.ticket_id, fresh)
        self._pump()
        return True

    def deliver(self, spec: WorkerSpec, inputs: list[tuple[str, str]], person: bool = False) -> bool:
        """Hand Inputs to a running Assignment: into a busy worker's inbox, or as a wake for an idle one."""
        with self._lock:
            state = self._states.get(spec.ticket_id)
            if state is None or state.assignment_id != spec.assignment_id:
                if state is not None:
                    state.cancel.set()
                state = _State(spec.assignment_id)
                self._states[spec.ticket_id] = state
            fresh = self._accept(state, inputs)
            if not fresh:
                return False
            if state.busy:
                state.inbox.extend(fresh)
            elif state.queued is not None:
                state.queued.inputs.extend(fresh)
                state.queued.person = state.queued.person or person
            else:
                wake = _Wake(spec, fresh, launch=False, person=person)
                state.queued = wake
                self._waiting.append(wake)
        self._delivered(spec.ticket_id, fresh)
        self._pump()
        return True

    def stop(self, ticket_id: str, assignment_id: str) -> None:
        """End an Assignment's work: a running turn stops at its next step and queued work is dropped."""
        with self._lock:
            state = self._states.get(ticket_id)
            if state is None or state.assignment_id != assignment_id:
                return
            state.cancel.set()
            state.queued = None
            del self._states[ticket_id]

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            for state in self._states.values():
                state.cancel.set()
                state.queued = None
            self._waiting.clear()
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": self._running,
                "queued": sum(1 for w in self._waiting if self._is_current(w)),
                "concurrency": self.concurrency,
                "busy_tickets": sorted(t for t, s in self._states.items() if s.busy),
            }

    # -- internals ---------------------------------------------------------------------------------------------

    @staticmethod
    def _accept(state: _State, inputs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        fresh = [(i, t) for i, t in inputs if i not in state.accepted]
        state.accepted.update(i for i, _ in fresh)
        return fresh

    def _delivered(self, ticket_id: str, inputs: list[tuple[str, str]]) -> None:
        if inputs:
            self.emit("delivered", ticket_id, [i for i, _ in inputs])

    def _is_current(self, wake: _Wake) -> bool:
        state = self._states.get(wake.spec.ticket_id)
        return state is not None and state.queued is wake

    def _pump(self) -> None:
        with self._lock:
            while True:
                stale = [w for w in self._waiting if not self._is_current(w)]
                for wake in stale:
                    self._waiting.remove(wake)
                if not self._waiting:
                    return
                person = next((w for w in self._waiting if w.person), None)
                if self._running < self.concurrency:
                    wake = person or self._waiting[0]
                elif self._running < self.concurrency + 1 and person is not None:
                    wake = person
                else:
                    return
                self._waiting.remove(wake)
                state = self._states[wake.spec.ticket_id]
                state.queued = None
                state.busy = True
                self._running += 1
                thread = threading.Thread(target=self._run, args=(wake, state), name=f"worker-{wake.spec.name}",
                                          daemon=True)
                self._threads = [t for t in self._threads if t.is_alive()] + [thread]
                thread.start()

    def _drain(self, state: _State) -> list[tuple[str, str]]:
        with self._lock:
            items = list(state.inbox)
            state.inbox.clear()
            return items

    def _run(self, wake: _Wake, state: _State) -> None:
        spec = wake.spec
        try:
            if wake.launch:
                error = self._prepare(spec)
                if error:
                    self.emit("report", spec.ticket_id, spec.assignment_id, Report("failed", error))
                    return
                self.emit("started", spec.ticket_id, spec.assignment_id)
            report = self.run(spec, self.client, wake.inputs, _Events(self), host=_Host(self, spec),
                              inbox=lambda: self._drain(state), cancelled=state.cancel.is_set)
            if report is not None and not state.cancel.is_set():
                self.emit("report", spec.ticket_id, spec.assignment_id, report)
        except Exception as exc:  # noqa: BLE001 - a crashed turn fails its ticket instead of the process
            log.exception("worker for %s crashed", spec.ticket_id)
            self.emit("report", spec.ticket_id, spec.assignment_id,
                      Report("failed", f"The worker crashed: {type(exc).__name__}: {exc}"))
        finally:
            with self._lock:
                state.busy = False
                self._running -= 1
                leftover = list(state.inbox)
                state.inbox.clear()
                if leftover and not state.cancel.is_set() and self._states.get(spec.ticket_id) is state:
                    again = _Wake(replace(spec), leftover, launch=False, person=True)
                    state.queued = again
                    self._waiting.appendleft(again)
            self._pump()

    def _prepare(self, spec: WorkerSpec) -> str:
        """Create the Ticket directory once, running the prepare command first. Returns an error text or empty."""
        if spec.directory.exists():
            return ""
        self.root.mkdir(parents=True, exist_ok=True)
        if self.prepare.strip():
            try:
                process = subprocess.run(["/bin/sh", "-c", self.prepare], cwd=self.root,
                                         env={**os.environ, **spec.env}, capture_output=True, text=True,
                                         timeout=self.prepare_timeout)
            except subprocess.TimeoutExpired:
                return f"The prepare command did not finish within {self.prepare_timeout} seconds."
            if process.returncode != 0:
                output = (process.stdout + process.stderr).strip()[-2000:]
                return f"The prepare command failed with exit code {process.returncode}:\n{output}"
        spec.directory.mkdir(parents=True, exist_ok=True)
        return ""
