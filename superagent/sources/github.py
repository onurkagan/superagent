"""The GitHub Source: Issues, pull requests and comments of one or more repositories, read and written through ``gh``.

For every repository it polls the Issues, Issue comments and review comments updated since that repository's
cursor, turns new ones into Triggers, and follows the state of pull requests that live Tickets follow. Messages
written by the agent account are dropped, and a message reaches the domain only when it addresses the agent, belongs
to a Thread a Ticket follows, or falls in a scope whose receive-all switch is on.

Thread keys name the repository: ``github:owner/name#12`` is Issue or pull request 12 of ``owner/name`` on the Source
instance named ``github``. Replies are posted as comments, or as review-thread replies for review comments.
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
REPOSITORY_PATTERN = re.compile(r"[\w.-]+/[\w.-]+")


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
    repositories: tuple[str, ...]
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


def split_reference(reference: str) -> tuple[str, int] | None:
    """Split ``owner/name#12`` into its repository and number."""
    repository, sep, number = reference.rpartition("#")
    if not sep or not number.isdigit() or not REPOSITORY_PATTERN.fullmatch(repository):
        return None
    return repository, int(number)


class GitHubSource:
    def __init__(self, config: GitHubConfig, store: Store, gh: GhClient | None = None,
                 prompts: PromptLibrary | None = None, now: Callable[[], str] = _utc_now, name: str = "github") -> None:
        if not config.repositories:
            raise ValueError("a GitHub source needs at least one repository")
        for repository in config.repositories:
            if not REPOSITORY_PATTERN.fullmatch(repository):
                raise ValueError(f"repository must look like owner/name, got {repository!r}")
        self.name = name
        self.config = config
        self.store = store
        self.gh = gh or GhClient()
        self.prompts = prompts or PromptLibrary()
        self.now = now
        self.poll_seconds = config.poll_seconds
        self._repositories = {r.lower(): r for r in config.repositories}
        self._account = config.account
        self._user_id: int | None = None
        self._pending: dict[str, str] = {}
        self._failures: dict[str, int] = {}
        self._errors: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    # -- identity and keys -------------------------------------------------------------------------------------

    @property
    def repositories(self) -> tuple[str, ...]:
        return tuple(self.config.repositories)

    def canonical(self, repository: str) -> str | None:
        """The configured spelling of a repository, or None when this Source does not watch it."""
        return self._repositories.get(repository.lower())

    def thread_key(self, repository: str, number: int) -> str:
        return f"{self.name}:{repository}#{number}"

    def parse_thread(self, thread_key: str) -> tuple[str, int] | None:
        prefix = f"{self.name}:"
        if not thread_key.startswith(prefix):
            return None
        parts = split_reference(thread_key[len(prefix):])
        if parts is None or self.canonical(parts[0]) is None:
            return None
        return self.canonical(parts[0]) or parts[0], parts[1]

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

    def _cursor_key(self, repository: str) -> str:
        return f"source:{self.name}:cursor:{repository.lower()}"

    def _count(self, name: str, amount: int = 1) -> None:
        self._counts[name] = self._counts.get(name, 0) + amount

    def poll(self, listened: set[str]) -> list[Trigger]:
        """Read every repository; one failing repository does not hold back the others."""
        triggers: list[Trigger] = []
        for repository in self.repositories:
            cursor = self.store.kv_get(self._cursor_key(repository))
            if cursor is None:
                # A newly watched repository starts from now instead of replaying its history.
                self._pending[repository] = self.now()
                continue
            try:
                found, newest = self._read_since(repository, cursor, listened)
            except GhError as exc:
                self._failures[repository] = self._failures.get(repository, 0) + 1
                self._errors[repository] = str(exc)
                continue
            self._failures.pop(repository, None)
            self._errors.pop(repository, None)
            self._pending[repository] = newest
            triggers.extend(found)
        return triggers

    def acknowledge(self) -> None:
        for repository, cursor in self._pending.items():
            self.store.kv_set(self._cursor_key(repository), cursor)
        self._pending = {}

    def _read_since(self, repository: str, cursor: str, listened: set[str]) -> tuple[list[Trigger], str]:
        newest = cursor
        triggers: list[Trigger] = []
        query = f"sort=updated&direction=asc&per_page=100&since={cursor}"
        for item in self.gh.api(f"repos/{repository}/issues?state=all&{query}") or []:
            newest = max(newest, str(item.get("updated_at", "")))
            try:
                key = self.thread_key(repository, int(item["number"]))
                if str(item.get("created_at", "")) >= cursor:
                    trigger = self._issue_trigger(repository, item, listened)
                    if trigger:
                        triggers.append(trigger)
                if "pull_request" in item and key in listened:
                    triggers.extend(self._pull_request_state(repository, item, key))
            except (KeyError, TypeError, ValueError):
                self._count("malformed")
        for item in self.gh.api(f"repos/{repository}/issues/comments?{query}") or []:
            newest = max(newest, str(item.get("updated_at", "")))
            if str(item.get("created_at", "")) >= cursor:
                trigger = self._safe(self._comment_trigger, repository, item, listened)
                if trigger:
                    triggers.append(trigger)
        for item in self.gh.api(f"repos/{repository}/pulls/comments?{query}") or []:
            newest = max(newest, str(item.get("updated_at", "")))
            if str(item.get("created_at", "")) >= cursor:
                trigger = self._safe(self._review_comment_trigger, repository, item, listened)
                if trigger:
                    triggers.append(trigger)
        return triggers, newest

    def _safe(self, build: Callable[[str, dict[str, Any], set[str]], Trigger | None], repository: str,
              item: dict[str, Any], listened: set[str]) -> Trigger | None:
        try:
            return build(repository, item, listened)
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

    def _issue_trigger(self, repository: str, item: dict[str, Any], listened: set[str]) -> Trigger | None:
        number = int(item["number"])
        key = self.thread_key(repository, number)
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
            id=f"{self.name}:{'pr' if is_pr else 'issue'}:{item['id']}", source=self.name,
            kind="pull_request" if is_pr else "issue", actor=actor, text=text, threads=(key,),
            channels=(ChannelRef(self.name, f"issue:{repository}#{number}"),), actor_is_bot=_is_bot(user),
            addressed="mention" if addressed else "", scope=scope, observed=observed,
            url=str(item.get("html_url") or ""), title=title, at=str(item.get("created_at") or ""),
            meta={"repository": repository, "number": number},
        )

    def _comment_trigger(self, repository: str, item: dict[str, Any], listened: set[str]) -> Trigger | None:
        number = _number_from_url(str(item["issue_url"]))
        key = self.thread_key(repository, number)
        user = item.get("user") or {}
        actor = str(user.get("login", ""))
        text = str(item.get("body") or "")
        addressed = self._mentions(text)
        receive, observed = self._admit(actor, addressed, key, "comments", listened)
        if not receive:
            return None
        return Trigger(
            id=f"{self.name}:comment:{item['id']}", source=self.name, kind="comment", actor=actor, text=text,
            threads=(key,), channels=(ChannelRef(self.name, f"issue:{repository}#{number}"),),
            actor_is_bot=_is_bot(user), addressed="mention" if addressed else "", scope="comments",
            observed=observed, url=str(item.get("html_url") or ""), at=str(item.get("created_at") or ""),
            meta={"repository": repository, "number": number},
        )

    def _review_comment_trigger(self, repository: str, item: dict[str, Any], listened: set[str]) -> Trigger | None:
        number = _number_from_url(str(item["pull_request_url"]))
        key = self.thread_key(repository, number)
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
            id=f"{self.name}:review-comment:{item['id']}", source=self.name, kind="review_comment", actor=actor,
            text=f"Review comment on {location}:\n{body}", threads=(key,),
            channels=(ChannelRef(self.name, f"pr:{repository}#{number}", reply_to=str(root)),),
            actor_is_bot=_is_bot(user), addressed="mention" if addressed else "", scope="comments",
            observed=observed, url=str(item.get("html_url") or ""), at=str(item.get("created_at") or ""),
            meta={"repository": repository, "number": number},
        )

    def _pull_request_state(self, repository: str, item: dict[str, Any], key: str) -> list[Trigger]:
        number = int(item["number"])
        closed_key = f"source:{self.name}:closed:{repository.lower()}#{number}"
        was_closed = bool(self.store.kv_get(closed_key, False))
        meta = {"repository": repository, "number": number}
        if item.get("state") == "closed" and not was_closed:
            pull = self.gh.api(f"repos/{repository}/pulls/{number}") or {}
            merged = bool(pull.get("merged"))
            self.store.kv_set(closed_key, True)
            closed_at = str(item.get("closed_at") or item.get("updated_at") or "")
            return [Trigger(id=f"{self.name}:pr-closed:{repository}#{number}:{closed_at}", source=self.name,
                            kind="pr_closed", actor="",
                            text=f"Pull request {repository}#{number} was {'merged' if merged else 'closed'}.",
                            threads=(key,), at=closed_at, meta={**meta, "merged": merged})]
        if item.get("state") == "open" and was_closed:
            self.store.kv_set(closed_key, False)
            at = str(item.get("updated_at") or "")
            return [Trigger(id=f"{self.name}:pr-reopened:{repository}#{number}:{at}", source=self.name,
                            kind="pr_reopened", actor="", text=f"Pull request {repository}#{number} was reopened.",
                            threads=(key,), at=at, meta=meta)]
        return []

    # -- writing -----------------------------------------------------------------------------------------------

    def send(self, channel: ChannelRef, text: str) -> None:
        body = f"{self.config.disclosure}\n\n{text}" if self.config.disclosure else text
        kind, _, reference = channel.address.partition(":")
        parts = split_reference(reference)
        repository = self.canonical(parts[0]) if parts else None
        if parts is None or repository is None or kind not in ("issue", "pr"):
            raise PermanentSendError(f"{self.name} cannot write to {channel.address!r}")
        number = parts[1]
        try:
            if kind == "pr" and channel.reply_to:
                try:
                    self.gh.api(f"repos/{repository}/pulls/{number}/comments/{channel.reply_to}/replies", "POST",
                                {"body": body})
                    return
                except GhError as exc:
                    if exc.status != 404:
                        raise
                    # The review comment is gone: answer in the pull request's conversation instead.
            self.gh.api(f"repos/{repository}/issues/{number}/comments", "POST", {"body": body})
        except GhError as exc:
            if exc.status in (410, 422) or (exc.status == 403 and "locked" in str(exc).lower()):
                raise PermanentSendError(str(exc)) from exc
            raise

    # -- references, variables and health ----------------------------------------------------------------------

    def resolve_thread(self, ref: str) -> str | None:
        """Accept a Thread key, ``owner/name#12``, an Issue or pull request URL, or ``#12`` when this Source watches
        exactly one repository."""
        ref = ref.strip()
        parsed = self.parse_thread(ref)
        if parsed:
            return self.thread_key(*parsed)
        match = re.fullmatch(r"#(\d+)", ref)
        if match:
            return self.thread_key(self.repositories[0], int(match.group(1))) if len(self.repositories) == 1 else None
        match = re.fullmatch(r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)(?:[/?#].*)?", ref, re.I)
        if match:
            repository = self.canonical(match.group(1))
            return self.thread_key(repository, int(match.group(2))) if repository else None
        parts = split_reference(ref)
        if parts:
            repository = self.canonical(parts[0])
            return self.thread_key(repository, parts[1]) if repository else None
        return None

    def listens(self, thread_key: str) -> bool:
        return self.parse_thread(thread_key) is not None

    def variables(self, origin: Trigger | None) -> list[Variable]:
        result = [
            Variable("SUPERAGENT_GITHUB_REPOSITORIES", ",".join(self.repositories),
                     f"Every repository the {self.name} source watches"),
            Variable("SUPERAGENT_GITHUB_ACCOUNT", self.account, "The agent's GitHub account; gh is logged in as it"),
        ]
        email = self.commit_email()
        if email:
            result.append(Variable("SUPERAGENT_GITHUB_COMMIT_EMAIL", email, "The commit email of the agent account"))
        repository = ""
        if origin is not None:
            parsed = self.parse_thread(origin.threads[0]) if origin.threads else None
            if parsed:
                repository = parsed[0]
                result.append(Variable("SUPERAGENT_GITHUB_NUMBER", str(parsed[1]),
                                       "The Issue or pull request number this ticket started from"))
        elif len(self.repositories) == 1:
            repository = self.repositories[0]
        if repository:
            result.append(Variable("SUPERAGENT_GITHUB_REPOSITORY", repository,
                                   "The repository this ticket works on (owner/name)"))
        return result

    def instructions(self) -> str:
        return fill(self.prompts.part("github"), disclosure=self.config.disclosure or "(no disclosure line)")

    def health(self) -> SourceHealth:
        failing = {r: n for r, n in self._failures.items() if n >= FAILURES_BEFORE_ERROR}
        detail = "; ".join(f"{r}: {self._errors.get(r, '')}" for r in failing)
        return SourceHealth(ok=not failing, detail=detail,
                            consecutive_failures=max(self._failures.values(), default=0), counts=dict(self._counts))
