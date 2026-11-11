"""Commands: how the panel, the CLI and the orchestrator reach the running agent.

Nothing outside the runtime's main loop calls the Engine. Callers queue a command in the store and the runtime applies
it on its next tick. A caller that needs the outcome uses :func:`call`, which adds a reply id; the runtime stores the
outcome under that id with :func:`respond`, and :func:`call` waits for it. An outcome that starts with ``error:`` is a
refusal.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from superagent.core.ids import new_id
from superagent.store import Store

REPLY_ARG = "_reply"
ERROR_PREFIX = "error: "


def _key(reply_id: str) -> str:
    return f"command-reply:{reply_id}"


def call(store: Store, kind: str, *, timeout: float = 30.0, poll: float = 0.2,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         **args: Any) -> str | None:
    """Queue a command and wait for its outcome. Returns None when the agent did not answer in time."""
    reply_id = new_id("cr")
    store.enqueue_command(kind, **{REPLY_ARG: reply_id}, **args)
    deadline = clock() + timeout
    while True:
        value = store.kv_get(_key(reply_id))
        if value is not None:
            store.kv_delete(_key(reply_id))
            return str(value)
        if clock() >= deadline:
            return None
        sleep(poll)


def respond(store: Store, args: dict[str, Any], outcome: str) -> None:
    """Store a command's outcome for the caller that waits on it, if any."""
    reply_id = args.get(REPLY_ARG)
    if reply_id:
        store.kv_set(_key(str(reply_id)), outcome)


def is_error(outcome: str | None) -> bool:
    return outcome is None or outcome.startswith(ERROR_PREFIX)
