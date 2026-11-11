"""Durable state in one SQLite file.

Domain records are stored as JSON documents keyed by kind and id, with a small ``tag`` column for the few filtered
lists the runtime needs (open Tickets, Threads with queued work). The store also holds the event journal, the Source
cursors (``kv``), the outbox and the command queue that the panel and CLI use to reach the runtime.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from superagent.core.model import Assignment, Decision, Eval, Thread, Ticket, Trigger
from superagent.core.serde import from_dict, to_dict

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS docs_tag ON docs (kind, tag);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    address TEXT NOT NULL,
    reply_to TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_status ON messages (status, next_at);
CREATE TABLE IF NOT EXISTS commands (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    args TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

_KINDS: dict[type, str] = {
    Trigger: "trigger",
    Thread: "thread",
    Eval: "eval",
    Decision: "decision",
    Ticket: "ticket",
    Assignment: "assignment",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _key(obj: Any) -> str:
    return obj.key if isinstance(obj, Thread) else obj.id


def _tag(obj: Any) -> str:
    if isinstance(obj, Ticket):
        return obj.state
    if isinstance(obj, Assignment):
        return obj.state
    if isinstance(obj, Thread):
        return "active" if (obj.queued or obj.eval_id) else ""
    return ""


class Store:
    """A thread-safe wrapper around one SQLite connection. ``atomic`` groups writes into one transaction."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- transactions ------------------------------------------------------------------------------------------

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- documents ---------------------------------------------------------------------------------------------

    def get(self, cls: type[T], key: str) -> T | None:
        rows = self.query("SELECT body FROM docs WHERE kind = ? AND id = ?", (_KINDS[cls], key))
        return from_dict(cls, json.loads(rows[0]["body"])) if rows else None

    def put(self, obj: Any) -> None:
        kind = _KINDS[type(obj)]
        body = json.dumps(to_dict(obj), ensure_ascii=False)
        self.execute(
            "INSERT INTO docs (kind, id, tag, body, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (kind, id) DO UPDATE SET tag = excluded.tag, body = excluded.body, "
            "updated_at = excluded.updated_at",
            (kind, _key(obj), _tag(obj), body, now_iso()),
        )

    def delete(self, cls: type, key: str) -> None:
        self.execute("DELETE FROM docs WHERE kind = ? AND id = ?", (_KINDS[cls], key))

    def list(self, cls: type[T], tag: str | None = None, limit: int | None = None, newest_first: bool = False) -> list[T]:
        sql = "SELECT body FROM docs WHERE kind = ?"
        params: list[Any] = [_KINDS[cls]]
        if tag is not None:
            sql += " AND tag = ?"
            params.append(tag)
        sql += " ORDER BY id DESC" if newest_first else " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [from_dict(cls, json.loads(r["body"])) for r in self.query(sql, tuple(params))]

    def count(self, cls: type, tag: str | None = None) -> int:
        if tag is None:
            rows = self.query("SELECT COUNT(*) AS n FROM docs WHERE kind = ?", (_KINDS[cls],))
        else:
            rows = self.query("SELECT COUNT(*) AS n FROM docs WHERE kind = ? AND tag = ?", (_KINDS[cls], tag))
        return int(rows[0]["n"])

    # -- key/value ---------------------------------------------------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value)))

    # -- event journal -----------------------------------------------------------------------------------------

    def journal(self, event_type: str, data: dict[str, Any]) -> None:
        self.execute("INSERT INTO events (at, type, data) VALUES (?, ?, ?)",
                     (now_iso(), event_type, json.dumps(data, ensure_ascii=False)))

    def events(self, limit: int = 100, since_iso: str | None = None) -> list[dict[str, Any]]:
        if since_iso:
            rows = self.query("SELECT * FROM events WHERE at >= ? ORDER BY seq DESC LIMIT ?", (since_iso, limit))
        else:
            rows = self.query("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
        return [{"seq": r["seq"], "at": r["at"], "type": r["type"], "data": json.loads(r["data"])} for r in rows]

    # -- command queue -----------------------------------------------------------------------------------------

    def enqueue_command(self, kind: str, **args: Any) -> None:
        """Queue a command for the running runtime, such as a person stopping a Ticket from the panel."""
        self.execute("INSERT INTO commands (kind, args, at) VALUES (?, ?, ?)", (kind, json.dumps(args), now_iso()))

    def take_commands(self) -> list[tuple[str, dict[str, Any]]]:
        with self.atomic():
            rows = self.query("SELECT seq, kind, args FROM commands ORDER BY seq")
            if rows:
                self.execute("DELETE FROM commands WHERE seq <= ?", (rows[-1]["seq"],))
        return [(r["kind"], json.loads(r["args"])) for r in rows]
