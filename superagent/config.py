"""Deployment settings, read from ``superagent.toml``.

Behaviour a team can describe in words lives in prompts, not here. The settings below are the few facts code needs:
which model to call, where to work, who may ask for work, and which Sources run. Unknown keys are errors, so a typo
never silently disables a protection.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from superagent.core.policy import ACCEPT_MODES, CELL_VALUES, Admission, Policy, Rule, SourcePolicy
from superagent.llm.client import DEFAULT_MODEL
from superagent.sources.github import DEFAULT_DISCLOSURE, REPOSITORY_PATTERN, SCOPES

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_BLOCKED_COMMANDS = ("sudo", "shutdown", "reboot", "mkfs", "dd")


class ConfigError(ValueError):
    pass


@dataclass
class ModelSettings:
    worker: str = DEFAULT_MODEL
    assessor: str = DEFAULT_MODEL
    worker_effort: str = "high"
    assessor_effort: str = "low"
    fallbacks: str = "default"
    base_url: str = ""


@dataclass
class ExecutionSettings:
    root: str = ""
    concurrency: int = 2
    prepare: str = ""
    blocked_commands: tuple[str, ...] = DEFAULT_BLOCKED_COMMANDS
    max_steps: int = 200


@dataclass
class PolicySettings:
    mode: str = "observe"
    eval_timeout_seconds: int = 600
    rules: list[Rule] = field(default_factory=list)


@dataclass
class SourceSettings:
    name: str = ""
    kind: str = ""
    enabled: bool = True
    allowed: tuple[str, ...] = ()
    bots: tuple[str, ...] = ()
    admission: Admission = field(default_factory=Admission)
    poll_seconds: float = 60.0

    def policy(self) -> SourcePolicy:
        return SourcePolicy(allowed=self.allowed, bots=self.bots, admission=self.admission)


@dataclass
class GitHubSettings(SourceSettings):
    repositories: tuple[str, ...] = ()
    account: str = ""
    disclosure: str = DEFAULT_DISCLOSURE
    receive_all: dict[str, bool] = field(default_factory=lambda: {s: False for s in SCOPES})


@dataclass
class InboxSettings(SourceSettings):
    path: str = ""
    replies: str = ""
    poll_seconds: float = 2.0


@dataclass
class PanelSettings:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


@dataclass
class Settings:
    base_dir: Path = field(default_factory=Path.cwd)
    state_dir: str = ".superagent"
    model: ModelSettings = field(default_factory=ModelSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    policy: PolicySettings = field(default_factory=PolicySettings)
    source_configs: dict[str, SourceSettings] = field(default_factory=dict)
    panel: PanelSettings = field(default_factory=PanelSettings)

    def _path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    @property
    def state_path(self) -> Path:
        return self._path(self.state_dir)

    @property
    def db_path(self) -> Path:
        return self.state_path / "superagent.db"

    @property
    def sessions_dir(self) -> Path:
        return self.state_path / "sessions"

    @property
    def prompts_dir(self) -> Path:
        return self.state_path / "prompts"

    @property
    def work_root(self) -> Path:
        return self._path(self.execution.root) if self.execution.root else self.state_path / "work"

    def sources(self) -> dict[str, SourceSettings]:
        """The enabled Source instances by name."""
        return {name: s for name, s in self.source_configs.items() if s.enabled}

    @property
    def github_sources(self) -> list[GitHubSettings]:
        return [s for s in self.sources().values() if isinstance(s, GitHubSettings)]

    @property
    def inbox(self) -> InboxSettings | None:
        """The first enabled inbox instance, which `superagent say` writes to."""
        return next((s for s in self.sources().values() if isinstance(s, InboxSettings)), None)

    def inbox_files(self, inbox: InboxSettings) -> tuple[Path, Path]:
        stem = "inbox" if inbox.name == "inbox" else inbox.name
        replies = "replies" if inbox.name == "inbox" else f"{inbox.name}-replies"
        path = self._path(inbox.path) if inbox.path else self.state_path / f"{stem}.jsonl"
        return path, self._path(inbox.replies) if inbox.replies else self.state_path / f"{replies}.jsonl"

    @property
    def inbox_path(self) -> Path:
        return self.inbox_files(self.inbox or InboxSettings(name="inbox"))[0]

    @property
    def replies_path(self) -> Path:
        return self.inbox_files(self.inbox or InboxSettings(name="inbox"))[1]

    def build_policy(self) -> Policy:
        return Policy(mode=self.policy.mode, rules=tuple(self.policy.rules),  # type: ignore[arg-type]
                      sources={name: s.policy() for name, s in self.sources().items()},
                      eval_timeout_seconds=self.policy.eval_timeout_seconds)


# -- parsing ---------------------------------------------------------------------------------------------------


def _take(table: dict[str, Any], where: str, known: set[str]) -> dict[str, Any]:
    unknown = set(table) - known
    if unknown:
        raise ConfigError(f"unknown setting(s) in [{where}]: {', '.join(sorted(unknown))}")
    return table


def _str(table: dict[str, Any], key: str, default: str, where: str, choices: tuple[str, ...] = ()) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"[{where}] {key} must be a string")
    if choices and value not in choices:
        raise ConfigError(f"[{where}] {key} must be one of {', '.join(choices)}; got {value!r}")
    return value


def _int(table: dict[str, Any], key: str, default: int, where: str, minimum: int = 0) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"[{where}] {key} must be an integer of at least {minimum}")
    return value


def _float(table: dict[str, Any], key: str, default: float, where: str) -> float:
    value = table.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"[{where}] {key} must be a positive number")
    return float(value)


def _bool(table: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{where}] {key} must be true or false")
    return value


def _strings(table: dict[str, Any], key: str, default: tuple[str, ...], where: str) -> tuple[str, ...]:
    value = table.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[{where}] {key} must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


def _admission(table: dict[str, Any], where: str) -> Admission:
    _take(table, where, {"listed_unaddressed", "unlisted_addressed", "unlisted_unaddressed"})
    defaults = Admission()
    values = {key: _str(table, key, getattr(defaults, key), where, CELL_VALUES) for key in
              ("listed_unaddressed", "unlisted_addressed", "unlisted_unaddressed")}
    for key in ("listed_unaddressed", "unlisted_unaddressed"):
        if values[key] == "explicit":
            raise ConfigError(f"[{where}] {key} cannot be explicit: only an addressed message makes an explicit request")
    return Admission(**values)  # type: ignore[arg-type]


_SOURCE_KEYS = {"kind", "enabled", "allowed", "bots", "admission", "poll_seconds"}
SOURCE_KINDS = ("github", "inbox")
_SOURCE_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")


def _source_common(name: str, kind: str, table: dict[str, Any], where: str, poll_default: float) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "enabled": _bool(table, "enabled", True, where),
        "allowed": _strings(table, "allowed", (), where),
        "bots": _strings(table, "bots", (), where),
        "admission": _admission(table.get("admission", {}), f"{where}.admission"),
        "poll_seconds": _float(table, "poll_seconds", poll_default, where),
    }


def _github(name: str, table: dict[str, Any]) -> GitHubSettings:
    where = f"sources.{name}"
    _take(table, where, _SOURCE_KEYS | {"repositories", "account", "disclosure", "receive_all"})
    receive = table.get("receive_all", {})
    _take(receive, f"{where}.receive_all", set(SCOPES))
    settings = GitHubSettings(
        **_source_common(name, "github", table, where, 60.0),
        repositories=_strings(table, "repositories", (), where),
        account=_str(table, "account", "", where),
        disclosure=_str(table, "disclosure", DEFAULT_DISCLOSURE, where),
        receive_all={s: _bool(receive, s, False, f"{where}.receive_all") for s in SCOPES},
    )
    if settings.enabled and not settings.repositories:
        raise ConfigError(f"[{where}] list at least one repository in repositories")
    for repository in settings.repositories:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ConfigError(f"[{where}] repository {repository!r} must look like owner/name")
    if len({r.lower() for r in settings.repositories}) != len(settings.repositories):
        raise ConfigError(f"[{where}] lists a repository twice")
    return settings


def _inbox(name: str, table: dict[str, Any]) -> InboxSettings:
    where = f"sources.{name}"
    _take(table, where, _SOURCE_KEYS | {"path", "replies"})
    return InboxSettings(**_source_common(name, "inbox", table, where, 2.0), path=_str(table, "path", "", where),
                         replies=_str(table, "replies", "", where))


def _sources(tables: dict[str, Any]) -> dict[str, SourceSettings]:
    """Parse ``[sources.<name>]`` tables. The kind defaults to the name for ``github`` and ``inbox``."""
    result: dict[str, SourceSettings] = {}
    for name, table in tables.items():
        if not _SOURCE_NAME.fullmatch(name):
            raise ConfigError(f"[sources.{name}] source names use lowercase letters, digits and dashes")
        if not isinstance(table, dict):
            raise ConfigError(f"[sources.{name}] must be a table")
        kind = table.get("kind", name if name in SOURCE_KINDS else "")
        if kind not in SOURCE_KINDS:
            raise ConfigError(f"[sources.{name}] kind must be one of {', '.join(SOURCE_KINDS)}")
        result[name] = _github(name, table) if kind == "github" else _inbox(name, table)
    owners: dict[str, str] = {}
    for name, settings in result.items():
        if isinstance(settings, GitHubSettings) and settings.enabled:
            for repository in settings.repositories:
                other = owners.setdefault(repository.lower(), name)
                if other != name:
                    raise ConfigError(f"repository {repository} is watched by both [sources.{other}] and "
                                      f"[sources.{name}]; a repository belongs to one source")
    return result


def _rules(items: Any) -> list[Rule]:
    if not isinstance(items, list):
        raise ConfigError("[[policy.rules]] must be an array of tables")
    rules = []
    seen = set()
    for index, item in enumerate(items):
        where = f"policy.rules[{index}]"
        _take(item, where, {"id", "name", "instruction", "enabled"})
        rule = Rule(id=_str(item, "id", "", where), name=_str(item, "name", "", where),
                    instruction=_str(item, "instruction", "", where), enabled=_bool(item, "enabled", True, where))
        if not rule.id or not rule.instruction.strip():
            raise ConfigError(f"[{where}] needs an id and an instruction")
        if rule.id in seen:
            raise ConfigError(f"[{where}] duplicate rule id {rule.id!r}")
        seen.add(rule.id)
        rules.append(Rule(rule.id, rule.name or rule.id, rule.instruction.strip(), rule.enabled))
    return rules


def parse_settings(data: dict[str, Any], base_dir: Path) -> Settings:
    _take(data, "top level", {"state_dir", "model", "execution", "policy", "sources", "panel"})
    model = _take(data.get("model", {}), "model",
                  {"worker", "assessor", "worker_effort", "assessor_effort", "fallbacks", "base_url"})
    execution = _take(data.get("execution", {}), "execution",
                      {"root", "concurrency", "prepare", "blocked_commands", "max_steps"})
    policy = _take(data.get("policy", {}), "policy", {"mode", "eval_timeout_seconds", "rules"})
    sources = data.get("sources", {})
    panel = _take(data.get("panel", {}), "panel", {"enabled", "host", "port", "token"})
    defaults = ModelSettings()
    settings = Settings(
        base_dir=base_dir,
        state_dir=_str(data, "state_dir", ".superagent", "top level"),
        model=ModelSettings(
            worker=_str(model, "worker", defaults.worker, "model"),
            assessor=_str(model, "assessor", defaults.assessor, "model"),
            worker_effort=_str(model, "worker_effort", defaults.worker_effort, "model", EFFORTS),
            assessor_effort=_str(model, "assessor_effort", defaults.assessor_effort, "model", EFFORTS),
            fallbacks=_str(model, "fallbacks", defaults.fallbacks, "model"),
            base_url=_str(model, "base_url", "", "model"),
        ),
        execution=ExecutionSettings(
            root=_str(execution, "root", "", "execution"),
            concurrency=_int(execution, "concurrency", 2, "execution", minimum=1),
            prepare=_str(execution, "prepare", "", "execution"),
            blocked_commands=_strings(execution, "blocked_commands", DEFAULT_BLOCKED_COMMANDS, "execution"),
            max_steps=_int(execution, "max_steps", 200, "execution", minimum=1),
        ),
        policy=PolicySettings(
            mode=_str(policy, "mode", "observe", "policy", ACCEPT_MODES),
            eval_timeout_seconds=_int(policy, "eval_timeout_seconds", 600, "policy", minimum=30),
            rules=_rules(policy.get("rules", [])),
        ),
        source_configs=_sources(sources),
        panel=PanelSettings(
            enabled=_bool(panel, "enabled", True, "panel"),
            host=_str(panel, "host", "127.0.0.1", "panel"),
            port=_int(panel, "port", 8765, "panel", minimum=1),
            token=_str(panel, "token", "", "panel"),
        ),
    )
    if not settings.sources():
        raise ConfigError("enable at least one source, such as [sources.github] or [sources.inbox]")
    return settings


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} does not exist; run `superagent init` to create it") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse_settings(data, path.resolve().parent)


CONFIG_TEMPLATE = '''# superagent configuration. See docs/006-deployment.md for every setting.
state_dir = ".superagent"

[model]
worker = "claude-opus-5-5"
assessor = "claude-opus-5-5"
worker_effort = "high"      # low | medium | high | xhigh | max
assessor_effort = "low"
fallbacks = "default"       # server-side refusal fallback; "" turns it off
base_url = ""               # empty: ANTHROPIC_BASE_URL or the Anthropic API

[execution]
concurrency = 2
# Runs once in the work root before a ticket's first assignment. $SUPERAGENT_NAME is the ticket's short name and
# $SUPERAGENT_GITHUB_REPOSITORY the repository the ticket belongs to.
prepare = 'gh repo clone "$SUPERAGENT_GITHUB_REPOSITORY" "$SUPERAGENT_NAME" -- --quiet && git -C "$SUPERAGENT_NAME" switch -c "$SUPERAGENT_NAME"'
blocked_commands = ["sudo", "shutdown", "reboot", "mkfs", "dd"]
max_steps = 200

[policy]
mode = "observe"            # off | observe | auto: what happens to proactive (inferred) candidates

# Proactive work needs a rule. Rules start in observe mode, which only records what would have been accepted.
[[policy.rules]]
id = "dependency-updates"
name = "Review dependency updates"
instruction = "When a listed bot opens a dependency update pull request, check that CI passes and fix small breakages."
enabled = false

# A source watches one or more repositories with one allowed list. Add more [sources.<name>] tables with
# kind = "github" when groups of repositories need different allowed lists.
[sources.github]
repositories = ["OWNER/NAME", "OWNER/OTHER"]
account = ""                # the agent's GitHub login; empty: the account gh is logged in as
allowed = ["YOUR-LOGIN"]    # people who may ask the agent for work by mentioning it
bots = []                   # bots whose messages may become proactive candidates, such as "dependabot[bot]"
poll_seconds = 60

[sources.github.receive_all]
issues = false
pull_requests = false
comments = false

[sources.inbox]
enabled = false             # a local inbox for demos: `superagent say "..."`
allowed = ["me"]

[panel]
host = "127.0.0.1"
port = 8765
'''
