"""A local Source: messages appended to a JSON Lines file, replies written to another.

It lets you give the agent work from the command line, and run superagent end to end without GitHub for demos, prompt
tuning and tests. Each line of the inbox is a JSON object such as
``{"actor": "alice", "text": "Please add a README", "thread": "main"}``; ``superagent say`` appends one. Inbox
messages address the agent unless ``"addressed": false`` is given. ``"repository": "owner/name"`` names the GitHub
repository a request is about; the ticket's worker then works on it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from superagent.core.model import ChannelRef, Trigger
from superagent.sources.base import SourceHealth, Variable
from superagent.store import Store


def append_message(path: Path, actor: str, text: str, thread: str = "main", bot: bool = False,
                   addressed: bool = True, repository: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"actor": actor, "text": text, "thread": thread, "bot": bot, "addressed": addressed,
            "at": datetime.now(UTC).isoformat(timespec="seconds")}
    if repository:
        line["repository"] = repository
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


class InboxSource:
    name = "inbox"

    def __init__(self, store: Store, path: Path, replies_path: Path, poll_seconds: float = 2.0) -> None:
        self.store = store
        self.path = Path(path)
        self.replies_path = Path(replies_path)
        self.poll_seconds = poll_seconds
        self._pending_offset: int | None = None
        self._health = SourceHealth()

    @property
    def _offset_key(self) -> str:
        return f"source:{self.name}:offset"

    def poll(self, listened: set[str]) -> list[Trigger]:
        offset = int(self.store.kv_get(self._offset_key, 0))
        if not self.path.is_file():
            return []
        triggers = []
        malformed = 0
        with self.path.open("rb") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                offset = handle.tell()
                try:
                    data = json.loads(raw)
                    thread = str(data.get("thread") or "main")
                    repository = str(data.get("repository") or "")
                    triggers.append(Trigger(
                        id=f"inbox:{start}", source=self.name, kind="message", actor=str(data["actor"]),
                        text=str(data["text"]), threads=(f"inbox:{thread}",),
                        channels=(ChannelRef(self.name, thread),), actor_is_bot=bool(data.get("bot")),
                        addressed="direct" if data.get("addressed", True) else "", scope="messages",
                        observed=not data.get("addressed", True), at=str(data.get("at") or ""),
                        title=f"{thread} ({repository})" if repository else thread,
                        meta={"repository": repository} if repository else {},
                    ))
                except (ValueError, KeyError, TypeError):
                    malformed += 1
        self._pending_offset = offset
        if malformed:
            self._health.counts["malformed"] = self._health.counts.get("malformed", 0) + malformed
        return triggers

    def acknowledge(self) -> None:
        if self._pending_offset is not None:
            self.store.kv_set(self._offset_key, self._pending_offset)
            self._pending_offset = None

    def send(self, channel: ChannelRef, text: str) -> None:
        self.replies_path.parent.mkdir(parents=True, exist_ok=True)
        line = {"thread": channel.address, "text": text, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        with self.replies_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def resolve_thread(self, ref: str) -> str | None:
        return ref if ref.startswith("inbox:") and len(ref) > len("inbox:") else None

    def listens(self, thread_key: str) -> bool:
        return thread_key.startswith("inbox:")

    def variables(self, origin: Trigger | None) -> list[Variable]:
        if origin is None:
            return []
        result = [Variable("SUPERAGENT_INBOX_THREAD", origin.threads[0].split(":", 1)[1],
                           "The inbox conversation this ticket started from")]
        repository = str((origin.meta or {}).get("repository") or "")
        if repository:
            result.append(Variable("SUPERAGENT_GITHUB_REPOSITORY", repository,
                                   "The repository this ticket works on (owner/name)"))
        return result

    def instructions(self) -> str:
        return ""

    def health(self) -> SourceHealth:
        return self._health
