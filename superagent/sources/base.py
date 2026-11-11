"""The Source port: a stream of events that becomes Triggers, and the Channel that writes back to it.

A Source drops the agent's own messages and everything outside what it listens to before they reach the domain,
keeps its own read position, and names the Threads and Channels of each Trigger. See docs/008-cookbook.md for how to
add one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from superagent.core.model import ChannelRef, Trigger


@dataclass(frozen=True)
class Variable:
    """An execution variable: set in the environment of worker commands and listed in the worker's prompt."""

    key: str
    value: str
    description: str


@dataclass
class SourceHealth:
    ok: bool = True
    detail: str = ""
    consecutive_failures: int = 0
    counts: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    name: str
    poll_seconds: float

    def poll(self, listened: set[str]) -> list[Trigger]:
        """Return new Triggers. ``listened`` holds the Thread keys live Tickets follow, which are always read."""

    def acknowledge(self) -> None:
        """Persist the read position after the runtime recorded the Triggers of the last poll."""

    def send(self, channel: ChannelRef, text: str) -> None:
        """Write a message; raise ``PermanentSendError`` when the conversation can never receive it."""

    def resolve_thread(self, ref: str) -> str | None:
        """Map a link or reference such as ``#12`` to a Thread key of this Source, or None."""

    def listens(self, thread_key: str) -> bool:
        """Whether the Source still reads this Thread."""

    def variables(self, origin: Trigger | None) -> list[Variable]:
        """Execution variables for a Ticket that started from ``origin``."""

    def instructions(self) -> str:
        """Extra prompt text for workers, such as how to use this Source's CLI; may be empty."""

    def health(self) -> SourceHealth: ...


class SourceRegistry:
    """The enabled Sources of a deployment, looked up by name or by the prefix of a Thread key."""

    def __init__(self, sources: list[Source] | None = None) -> None:
        self._sources: dict[str, Source] = {s.name: s for s in (sources or [])}

    def __iter__(self):
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def get(self, name: str) -> Source | None:
        return self._sources.get(name)

    def for_thread(self, thread_key: str) -> Source | None:
        return self._sources.get(thread_key.split(":", 1)[0])

    def resolve_thread(self, ref: str) -> str | None:
        for source in self._sources.values():
            key = source.resolve_thread(ref)
            if key:
                return key
        return None

    def listens(self, thread_key: str) -> bool:
        source = self.for_thread(thread_key)
        return bool(source and source.listens(thread_key))

    def listens_channel(self, channel: ChannelRef) -> bool:
        return channel.source in self._sources

    def variables(self, origin: Trigger | None) -> list[Variable]:
        """Every Source's variables; only the Source the Ticket started on sees its originating Trigger."""
        result: list[Variable] = []
        for source in self._sources.values():
            own_origin = origin if origin is not None and origin.source == source.name else None
            result.extend(source.variables(own_origin))
        return result
