"""The GitHub Source: Issues, pull requests and comments of one repository, read and written through ``gh``.

It polls the repository's Issues, Issue comments and review comments updated since its cursor, turns new ones into
Triggers, and follows the state of pull requests that live Tickets follow. Messages written by the agent account are
dropped, and a message reaches the domain only when it addresses the agent, belongs to a Thread a Ticket follows, or
falls in a scope whose receive-all switch is on. Replies are posted as comments, or as review-thread replies for
review comments.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from superagent.agents.prompts import PromptLibrary, fill
from superagent.core.model import ChannelRef, Trigger
from superagent.outbox import PermanentSendError
from superagent.sources.base import SourceHealth, Variable
from superagent.store import Store

DEFAULT_DISCLOSURE = "This is by an automated agent, not a human."
FAILURES_BEFORE_ERROR = 3
SCOPES = ("issues", "pull_requests", "comments")


class GhError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


class GhClient:
    """A thin wrapper over ``gh api`` so authentication stays with the GitHub CLI."""

    def __init__(self, gh: str = "gh", runner: Callable[..., Any] = subprocess.run, timeout: float = 60.0) -> None:
        self.gh = gh
        self.runner = runner
        self.timeout = timeout

    def api(self, path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        args = [self.gh, "api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        try:
            process = self.runner(args, input=stdin, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError as exc:
            raise GhError("the GitHub CLI (gh) is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh api {path} timed out") from exc
        if process.returncode != 0:
            message = (process.stderr or process.stdout or "").strip()
            match = _HTTP_STATUS.search(message)
            raise GhError(f"gh api {method} {path}: {message[:300]}", int(match.group(1)) if match else None)
        return json.loads(process.stdout) if process.stdout.strip() else None


@dataclass
class GitHubConfig:
    repository: str
    account: str = ""
    receive_all: dict[str, bool] = field(default_factory=lambda: {s: False for s in SCOPES})
    poll_seconds: float = 60.0
    disclosure: str = DEFAULT_DISCLOSURE


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_bot(user: dict[str, Any]) -> bool:
    login = str(user.get("login", ""))
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _number_from_url(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


class GitHubSource:
    name = "github"

    def __init__(self, config: GitHubConfig, store: Store, gh: GhClient | None = None,
                 prompts: PromptLibrary | None = None, now: Callable[[], str] = _utc_now) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", config.repository or ""):
            raise ValueError(f"repository must look like owner/name, got {config.repository!r}")
        self.config = config
        self.store = store
        self.gh = gh or GhClient()
        self.prompts = prompts or PromptLibrary()
        self.now = now
        self.poll_seconds = config.poll_seconds
        self._account = config.account
        self._user_id: int | None = None
        self._pending_cursor: str | None = None
        self._health = SourceHealth()

    # -- identity ----------------------------------------------------------------------------------------------

    @property
    def repository(self) -> str:
        return self.config.repository

    @property
    def account(self) -> str:
        if not self._account:
            user = self.gh.api("user")
            self._account = str(user["login"])
            self._user_id = int(user["id"])
        return self._account

    def commit_email(self) -> str:
        if self._user_id is None:
            try:
                user = self.gh.api(f"users/{self.account}")
                self._user_id = int(user["id"])
            except (GhError, KeyError, TypeError, ValueError):
                return ""
        return f"{self._user_id}+{self.account}@users.noreply.github.com"

    def _mentions(self, text: str) -> bool:
        pattern = rf"(?<![\w/-])@{re.escape(self.account)}(?![\w-])"
        return re.search(pattern, text or "", re.IGNORECASE) is not None

    # -- reading -----------------------------------------------------------------------------------------------

    @property
    def _cursor_key(self) -> str:
        return f"source:{self.name}:cursor"

    def _count(self, name: str, amount: int = 1) -> None:
        self._health.counts[name] = self._health.counts.get(name, 0) + amount

    def poll(self, listened: set[str]) -> list[Trigger]:
        cursor = self.store.kv_get(self._cursor_key)
        if cursor is None:
            # First run: start from now instead of replaying the repository's history.
            self._pending_cursor = self.now()
            return []
        try:
            triggers, newest = self._read_since(cursor, listened)
        except GhError as exc:
            self._health.consecutive_failures += 1
            self._health.detail = str(exc)
            self._health.ok = self._health.consecutive_failures < FAILURES_BEFORE_ERROR
            return []
        self._health = SourceHealth(ok=True, counts=self._health.counts)
        self._pending_cursor = newest
        return triggers

    def acknowledge(self) -> None:
        if self._pending_cursor is not None:
            self.store.kv_set(self._cursor_key, self._pending_cursor)
            self._pending_cursor = None

    def _read_since(self, cursor: str, listened: set[str]) -> tuple[list[Trigger], str]:
        repo = self.repository
        newest = cursor
        triggers: list[Trigger] = []
        query = f"sort=updated&direction=asc&per_page=100&since={cursor}"
        for item in self.gh.api(f"repos/{repo}/issues?state=all&{query}") or []:
            newest = max(newest, str(item.get("updated_at", "")))
            try:
                key = f"github:issue:{item['number']}"
                if str(item.get("created_at", "")) >= cursor:
                    trigger = self._issue_trigger(item, listened)
                    if trigger:
                        triggers.append(trigger)
                if "pull_request" in item and key in listened:
                    triggers.extend(self._pull_request_state(item, key))
            except (KeyError, TypeError, ValueError):
                self._count("malformed")
        for item in self.gh.api(f"repos/{repo}/issues/comments?{query}") or []:
            newest = max(newest, str(item.get("updated_at", "")))
            if str(item.get("created_at", "")) >= cursor:
                trigger = self._safe(self._comment_trigger, item, listened)
                if trigger:
                    triggers.append(trigger)
        for item in self.gh.api(f"repos/{repo}/pulls/comments?{query}") or []:
            newest = max(newest, str(item.get("updated_at", "")))
            if str(item.get("created_at", "")) >= cursor:
                trigger = self._safe(self._review_comment_trigger, item, listened)
                if trigger:
                    triggers.append(trigger)
        return triggers, newest

    def _safe(self, build: Callable[[dict[str, Any], set[str]], Trigger | None], item: dict[str, Any],
              listened: set[str]) -> Trigger | None:
        try:
            return build(item, listened)
        except (KeyError, TypeError, ValueError):
            self._count("malformed")
            return None

    def _admit(self, actor: str, addressed: bool, key: str, scope: str, listened: set[str]) -> tuple[bool, bool]:
        """Whether the Source receives the message, and whether it was received only by receive-all."""
        if actor.lower() == self.account.lower():
            self._count("own_messages")
            return False, False
        if addressed or key in listened:
            return True, False
        if self.config.receive_all.get(scope):
            return True, True
        self._count("outside_scope")
        return False, False

    def _issue_trigger(self, item: dict[str, Any], listened: set[str]) -> Trigger | None:
        number = int(item["number"])
        key = f"github:issue:{number}"
        is_pr = "pull_request" in item
        user = item.get("user") or {}
        actor = str(user.get("login", ""))
        title = str(item.get("title") or "")
        text = f"{title}\n\n{item.get('body') or ''}".strip()
        assignees = {str(a.get("login", "")).lower() for a in item.get("assignees") or []}
        addressed = self._mentions(text) or self.account.lower() in assignees
        scope = "pull_requests" if is_pr else "issues"
        receive, observed = self._admit(actor, addressed, key, scope, listened)
        if not receive:
            return None
        return Trigger(
            id=f"github:{'pr' if is_pr else 'issue'}:{item['id']}", source=self.name,
            kind="pull_request" if is_pr else "issue", actor=actor, text=text, threads=(key,),
            channels=(ChannelRef(self.name, f"issue:{number}"),), actor_is_bot=_is_bot(user),
            addressed="mention" if addressed else "", scope=scope, observed=observed,
            url=str(item.get("html_url") or ""), title=title, at=str(item.get("created_at") or ""),
            meta={"number": number},
        )

    def _comment_trigger(self, item: dict[str, Any], listened: set[str]) -> Trigger | None:
        number = _number_from_url(str(item["issue_url"]))
        key = f"github:issue:{number}"
        user = item.get("user") or {}
        actor = str(user.get("login", ""))
        text = str(item.get("body") or "")
        addressed = self._mentions(text)
        receive, observed = self._admit(actor, addressed, key, "comments", listened)
        if not receive:
            return None
        return Trigger(
            id=f"github:comment:{item['id']}", source=self.name, kind="comment", actor=actor, text=text,
            threads=(key,), channels=(ChannelRef(self.name, f"issue:{number}"),), actor_is_bot=_is_bot(user),
            addressed="mention" if addressed else "", scope="comments", observed=observed,
            url=str(item.get("html_url") or ""), at=str(item.get("created_at") or ""), meta={"number": number},
        )

    def _review_comment_trigger(self, item: dict[str, Any], listened: set[str]) -> Trigger | None:
        number = _number_from_url(str(item["pull_request_url"]))
        key = f"github:issue:{number}"
        user = item.get("user") or {}
        actor = str(user.get("login", ""))
        body = str(item.get("body") or "")
        addressed = self._mentions(body)
        receive, observed = self._admit(actor, addressed, key, "comments", listened)
        if not receive:
            return None
        location = f"{item.get('path', '')}:{item.get('line') or item.get('original_line') or ''}".rstrip(":")
        root = item.get("in_reply_to_id") or item["id"]
        return Trigger(
            id=f"github:review-comment:{item['id']}", source=self.name, kind="review_comment", actor=actor,
            text=f"Review comment on {location}:\n{body}", threads=(key,),
            channels=(ChannelRef(self.name, f"pr:{number}", reply_to=str(root)),), actor_is_bot=_is_bot(user),
            addressed="mention" if addressed else "", scope="comments", observed=observed,
            url=str(item.get("html_url") or ""), at=str(item.get("created_at") or ""), meta={"number": number},
        )

    def _pull_request_state(self, item: dict[str, Any], key: str) -> list[Trigger]:
        number = int(item["number"])
        closed_key = f"source:{self.name}:closed:{number}"
        was_closed = bool(self.store.kv_get(closed_key, False))
        if item.get("state") == "closed" and not was_closed:
            pull = self.gh.api(f"repos/{self.repository}/pulls/{number}") or {}
            merged = bool(pull.get("merged"))
            self.store.kv_set(closed_key, True)
            closed_at = str(item.get("closed_at") or item.get("updated_at") or "")
            return [Trigger(id=f"github:pr-closed:{number}:{closed_at}", source=self.name, kind="pr_closed",
                            actor="", text=f"Pull request #{number} was {'merged' if merged else 'closed'}.",
                            threads=(key,), at=closed_at, meta={"number": number, "merged": merged})]
        if item.get("state") == "open" and was_closed:
            self.store.kv_set(closed_key, False)
            at = str(item.get("updated_at") or "")
            return [Trigger(id=f"github:pr-reopened:{number}:{at}", source=self.name, kind="pr_reopened", actor="",
                            text=f"Pull request #{number} was reopened.", threads=(key,), at=at,
                            meta={"number": number})]
        return []

    # -- writing -----------------------------------------------------------------------------------------------

    def send(self, channel: ChannelRef, text: str) -> None:
        body = f"{self.config.disclosure}\n\n{text}" if self.config.disclosure else text
        kind, _, number = channel.address.partition(":")
        if not number.isdigit():
            raise PermanentSendError(f"unknown GitHub address {channel.address!r}")
        try:
            if kind == "pr" and channel.reply_to:
                try:
                    self.gh.api(f"repos/{self.repository}/pulls/{number}/comments/{channel.reply_to}/replies",
                                "POST", {"body": body})
                    return
                except GhError as exc:
                    if exc.status != 404:
                        raise
                    # The review comment is gone: answer in the pull request's conversation instead.
            self.gh.api(f"repos/{self.repository}/issues/{number}/comments", "POST", {"body": body})
        except GhError as exc:
            if exc.status in (410, 422) or (exc.status == 403 and "locked" in str(exc).lower()):
                raise PermanentSendError(str(exc)) from exc
            raise

    # -- references, variables and health ----------------------------------------------------------------------

    def resolve_thread(self, ref: str) -> str | None:
        ref = ref.strip()
        if re.fullmatch(r"github:issue:\d+", ref):
            return ref
        match = re.fullmatch(r"#(\d+)", ref)
        if match:
            return f"github:issue:{match.group(1)}"
        pattern = rf"https?://github\.com/{re.escape(self.repository)}/(?:issues|pull)/(\d+)(?:[/?#].*)?"
        match = re.fullmatch(pattern, ref, re.IGNORECASE)
        return f"github:issue:{match.group(1)}" if match else None

    def listens(self, thread_key: str) -> bool:
        return thread_key.startswith("github:issue:")

    def variables(self, origin: Trigger | None) -> list[Variable]:
        result = [
            Variable("SUPERAGENT_GITHUB_REPOSITORY", self.repository, "The repository the agent works on (owner/name)"),
            Variable("SUPERAGENT_GITHUB_ACCOUNT", self.account, "The agent's GitHub account; gh is logged in as it"),
        ]
        email = self.commit_email()
        if email:
            result.append(Variable("SUPERAGENT_GITHUB_COMMIT_EMAIL", email, "The commit email of the agent account"))
        if origin is not None and origin.threads:
            number = origin.threads[0].rsplit(":", 1)[-1]
            result.append(Variable("SUPERAGENT_GITHUB_NUMBER", number,
                                   "The Issue or pull request number this ticket started from"))
        return result

    def instructions(self) -> str:
        return fill(self.prompts.part("github"), disclosure=self.config.disclosure or "(no disclosure line)",
                    repository=self.repository)

    def health(self) -> SourceHealth:
        return self._health
