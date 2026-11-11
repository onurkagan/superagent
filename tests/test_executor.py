import queue
import threading
import time

import pytest

from superagent.agents.executor import Executor
from superagent.agents.worker import WorkerSpec
from superagent.core.model import Report


def spec_for(tmp_path, ticket="tk_1", assignment="as_1", name="fix"):
    return WorkerSpec(ticket_id=ticket, assignment_id=assignment, name=name, directory=tmp_path / "work" / name,
                      session_path=tmp_path / "sessions" / f"{ticket}.json", system="s", model="m",
                      env={"SUPERAGENT_NAME": name})


def drain(events, until, timeout=5.0):
    """Collect events until one of kind ``until`` arrives."""
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = events.get(timeout=0.05)
        except queue.Empty:
            continue
        seen.append(event)
        if event[0] == until:
            return seen
    raise AssertionError(f"no {until!r} event; saw {seen}")


class Gate:
    """A fake turn that blocks until released and records what it received."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = []
        self.started = threading.Semaphore(0)

    def __call__(self, spec, client, inputs, events, host=None, inbox=lambda: [], cancelled=lambda: False):
        self.calls.append((spec.ticket_id, [i for i, _ in inputs]))
        events.inputs_read(spec.ticket_id, [i for i, _ in inputs])
        self.started.release()
        self.release.wait(5)
        extra = inbox()
        if extra:
            events.inputs_read(spec.ticket_id, [i for i, _ in extra])
        return None if cancelled() else Report("waiting", "done")


@pytest.fixture
def events():
    return queue.Queue()


def test_launch_prepares_directory_runs_turn_and_reports(tmp_path, events):
    def turn(spec, client, inputs, ev, **kw):
        assert spec.directory.is_dir()
        assert (spec.directory.parent / "prepared.txt").read_text().strip() == "fix"
        return Report("ready", "done")

    executor = Executor(None, events, root=tmp_path / "work", prepare='echo "$SUPERAGENT_NAME" > prepared.txt',
                        run=turn)
    assert executor.start(spec_for(tmp_path), [("in_1", "go")])
    seen = drain(events, "report")
    assert [e[0] for e in seen] == ["delivered", "started", "report"]
    assert seen[-1][3].status == "ready"
    assert not executor.start(spec_for(tmp_path), [("in_1", "go")])


def test_failed_prepare_fails_the_ticket(tmp_path, events):
    executor = Executor(None, events, root=tmp_path / "work", prepare="echo nope >&2; exit 4",
                        run=lambda *a, **k: Report("ready", "x"))
    executor.start(spec_for(tmp_path), [])
    report = drain(events, "report")[-1][3]
    assert report.status == "failed" and "exit code 4" in report.summary and "nope" in report.summary


def test_inputs_for_a_busy_worker_go_to_its_inbox(tmp_path, events):
    gate = Gate()
    executor = Executor(None, events, root=tmp_path / "work", run=gate)
    spec = spec_for(tmp_path)
    executor.start(spec, [("in_1", "go")])
    assert gate.started.acquire(timeout=5)
    executor.deliver(spec, [("in_2", "more")], person=True)
    gate.release.set()
    seen = drain(events, "report")
    reads = [e[2] for e in seen if e[0] == "read"]
    assert reads == [["in_1"], ["in_2"]]


def test_concurrency_limit_with_an_extra_slot_for_people(tmp_path, events):
    gate = Gate()
    executor = Executor(None, events, root=tmp_path / "work", concurrency=1, run=gate)
    executor.start(spec_for(tmp_path, "tk_1", "as_1", "one"), [("a", "x")])
    assert gate.started.acquire(timeout=5)
    executor.start(spec_for(tmp_path, "tk_2", "as_2", "two"), [("b", "x")])
    time.sleep(0.1)
    assert executor.status()["running"] == 1 and executor.status()["queued"] == 1
    executor.deliver(spec_for(tmp_path, "tk_3", "as_3", "three"), [("c", "x")], person=True)
    assert gate.started.acquire(timeout=5)
    assert executor.status()["running"] == 2
    gate.release.set()
    deadline = time.time() + 5
    while len(gate.calls) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert [c[0] for c in gate.calls] == ["tk_1", "tk_3", "tk_2"]


def test_duplicate_inputs_are_accepted_once(tmp_path, events):
    gate = Gate()
    gate.release.set()
    executor = Executor(None, events, root=tmp_path / "work", run=gate)
    spec = spec_for(tmp_path)
    executor.start(spec, [("in_1", "go")])
    drain(events, "report")
    assert executor.deliver(spec, [("in_2", "x")])
    assert not executor.deliver(spec, [("in_2", "x")])


def test_stop_cancels_a_running_turn(tmp_path, events):
    gate = Gate()
    executor = Executor(None, events, root=tmp_path / "work", run=gate)
    spec = spec_for(tmp_path)
    executor.start(spec, [("in_1", "go")])
    assert gate.started.acquire(timeout=5)
    executor.stop(spec.ticket_id, spec.assignment_id)
    gate.release.set()
    time.sleep(0.2)
    kinds = []
    while not events.empty():
        kinds.append(events.get()[0])
    assert "report" not in kinds
    assert executor.status()["running"] == 0


def test_crashing_turn_reports_failure(tmp_path, events):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    executor = Executor(None, events, root=tmp_path / "work", run=boom)
    executor.start(spec_for(tmp_path), [])
    report = drain(events, "report")[-1][3]
    assert report.status == "failed" and "kaboom" in report.summary


def test_host_validates_replies_and_resolves_threads(tmp_path, events):
    captured = {}

    def turn(spec, client, inputs, ev, host=None, **kw):
        captured["long"] = host.reply("word " * 200, "result", "")
        captured["bad"] = host.reply("hi", "gossip", "")
        captured["ok"] = host.reply("Done: [PR #7](url).", "result", "t1")
        captured["follow"] = host.follow("#9")
        host.hand_back("t2", "beyond scope")
        return Report("waiting", "x")

    executor = Executor(None, events, root=tmp_path / "work", run=turn,
                        resolve_thread=lambda ref: "github:o/r#9" if ref == "#9" else None)
    executor.start(spec_for(tmp_path), [])
    seen = drain(events, "report")
    assert "limit" in captured["long"] and "purpose" in captured["bad"] and captured["ok"] == ""
    assert captured["follow"] == "github:o/r#9"
    kinds = [e[0] for e in seen]
    assert kinds.count("reply") == 1 and "follow" in kinds and "hand_back" in kinds
