from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from superagent.core.engine import Engine
from superagent.core.model import ChannelRef, Trigger
from superagent.core.policy import Admission, Policy, Rule, SourcePolicy
from superagent.store import Store

_counter = itertools.count(1)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def make_policy(mode="observe", rules=(), admission=Admission(), allowed=("alice",), bots=("dependabot[bot]",),
                timeout=600) -> Policy:
    return Policy(mode=mode, rules=tuple(rules), eval_timeout_seconds=timeout,
                  sources={"github": SourcePolicy(allowed=tuple(allowed), bots=tuple(bots), admission=admission)})


def make_trigger(text="@agent please fix the typo", actor="alice", thread="github:o/r#1", addressed="mention",
                 bot=False, observed=False, kind="comment", tid=None, meta=None, title="") -> Trigger:
    reference = thread.split(":", 1)[1]
    return Trigger(
        id=tid or f"github:comment:{next(_counter)}", source="github", kind=kind, actor=actor, text=text,
        threads=(thread,), channels=(ChannelRef("github", f"issue:{reference}"),), actor_is_bot=bot,
        addressed=addressed, observed=observed, title=title, meta=meta or {},
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def engine(store, clock) -> Engine:
    def resolve(ref: str) -> str | None:
        if ref.startswith("github:o/r#"):
            return ref
        if "/pull/" in ref or "/issues/" in ref:
            return "github:o/r#" + ref.rstrip("/").rsplit("/", 1)[-1]
        if ref.startswith("#"):
            return "github:o/r" + ref
        return None

    return Engine(store, make_policy(rules=[Rule("typos", "Fix typos", "Fix obvious typos in docs.")]),
                  clock=clock, resolve_thread=resolve)
