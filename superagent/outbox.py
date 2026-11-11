"""Outgoing messages: stored first, sent afterwards, retried with backoff and kept in order per conversation.

A Ticket's messages to one conversation go out in the order they were queued, so an acknowledgement never arrives
after the result it announced. An earlier message holds back later ones until it has failed three times in a row and
shows as a problem. Messages of other Tickets do not wait. See docs/003-execution.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from superagent.core.model import ChannelRef
from superagent.store import Store, now_iso

BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0
PROBLEM_AFTER_ATTEMPTS = 3


class PermanentSendError(Exception):
    """The conversation can never receive the message, such as a deleted Issue or a locked conversation."""


class Sender(Protocol):
    def send(self, channel: ChannelRef, text: str) -> None: ...


@dataclass(frozen=True)
class OutboundMessage:
    id: str
    channel: ChannelRef
    text: str
    kind: str
    ticket_id: str = ""


@dataclass(frozen=True)
class FlushResult:
    sent: int = 0
    failed: int = 0
    dropped: int = 0


class Outbox:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def send(self, message: OutboundMessage) -> None:
        """Queue a message idempotently; an id queued before keeps its first message."""
        self.store.execute(
            "INSERT OR IGNORE INTO messages (id, ticket_id, source, address, reply_to, text, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, message.ticket_id, message.channel.source, message.channel.address,
             message.channel.reply_to, message.text, message.kind, now_iso()),
        )

    def status(self, message_id: str) -> str | None:
        rows = self.store.query("SELECT status FROM messages WHERE id = ?", (message_id,))
        return rows[0]["status"] if rows else None

    def withdraw(self, message_id: str) -> None:
        self.store.execute("UPDATE messages SET status = 'dropped', last_error = 'withdrawn' "
                           "WHERE id = ? AND status = 'pending'", (message_id,))

    def flush(
        self,
        sender_for: Callable[[str], Sender | None],
        listens: Callable[[ChannelRef], bool] = lambda channel: True,
        limit: int = 50,
    ) -> FlushResult:
        """Try every due message once. ``sender_for`` returns None while a Source is off: its messages wait."""
        now = self.clock()
        sent = failed = dropped = 0
        held: set[tuple[str, str, str]] = set()
        rows = self.store.query("SELECT * FROM messages WHERE status = 'pending' ORDER BY seq LIMIT ?", (limit,))
        for row in rows:
            conversation = (row["ticket_id"], row["source"], row["address"])
            if row["ticket_id"] and conversation in held:
                continue
            channel = ChannelRef(row["source"], row["address"], row["reply_to"])
            blocks_later = row["attempts"] < PROBLEM_AFTER_ATTEMPTS
            if row["next_at"] > now:
                if blocks_later:
                    held.add(conversation)
                continue
            sender = sender_for(row["source"])
            if sender is None:
                held.add(conversation)
                continue
            if not listens(channel):
                self._drop(row["id"], "the source no longer listens to this conversation")
                dropped += 1
                continue
            try:
                sender.send(channel, row["text"])
            except PermanentSendError as exc:
                self._drop(row["id"], str(exc) or "the conversation cannot receive messages")
                dropped += 1
            except Exception as exc:  # noqa: BLE001 - any other failure is retried
                attempts = row["attempts"] + 1
                delay = min(BASE_DELAY_SECONDS * 2 ** (attempts - 1), MAX_DELAY_SECONDS)
                self.store.execute(
                    "UPDATE messages SET attempts = ?, next_at = ?, last_error = ? WHERE id = ?",
                    (attempts, now + delay, f"{type(exc).__name__}: {exc}"[:500], row["id"]),
                )
                failed += 1
                if attempts < PROBLEM_AFTER_ATTEMPTS:
                    held.add(conversation)
            else:
                self.store.execute("UPDATE messages SET status = 'sent', sent_at = ?, last_error = '' WHERE id = ?",
                                   (now_iso(), row["id"]))
                sent += 1
        return FlushResult(sent, failed, dropped)

    def _drop(self, message_id: str, reason: str) -> None:
        self.store.execute("UPDATE messages SET status = 'dropped', last_error = ? WHERE id = ?", (reason, message_id))

    def problems(self) -> list[dict]:
        """Messages that failed at least three times in a row and have not gone out."""
        rows = self.store.query("SELECT * FROM messages WHERE status = 'pending' AND attempts >= ? ORDER BY seq",
                                (PROBLEM_AFTER_ATTEMPTS,))
        return [dict(r) for r in rows]

    def recent(self, limit: int = 50, ticket_id: str | None = None) -> list[dict]:
        if ticket_id:
            rows = self.store.query("SELECT * FROM messages WHERE ticket_id = ? ORDER BY seq DESC LIMIT ?",
                                    (ticket_id, limit))
        else:
            rows = self.store.query("SELECT * FROM messages ORDER BY seq DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
