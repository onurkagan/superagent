"""Prompt parts and their assembly.

Every part has a built-in default under ``superagent/prompts``. A team overrides a part by placing a file with the
same relative path in its prompts directory, such as ``.superagent/prompts/team/scope.md``. Templates must keep their
placeholders. Prompt text changes what the model reads; it never changes the gate, Mandates or tool checks, which
code enforces. See docs/004-prompts.md.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from superagent.core.policy import Policy, Rule
from superagent.sources.base import Variable

PARTS: tuple[str, ...] = (
    "shared",
    "routing",
    "assessor",
    "orchestrator",
    "worker",
    "team/communication",
    "team/scope",
    "team/delivery",
    "environment",
    "policy",
    "policy-none",
    "github",
)
TEAM_PARTS: tuple[str, ...] = ("team/communication", "team/scope", "team/delivery")
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "environment": ("{variables}",),
    "policy": ("{version}", "{mode}", "{rules}"),
    "policy-none": ("{version}", "{mode}"),
    "github": ("{disclosure}",),
}


class PromptError(ValueError):
    """An override is unknown or lost a required placeholder."""


def fill(template: str, **values: str) -> str:
    """Replace ``{name}`` placeholders without interpreting other braces in the text."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


class PromptLibrary:
    def __init__(self, override_dir: str | Path | None = None) -> None:
        self.override_dir = Path(override_dir) if override_dir else None

    @staticmethod
    def default(key: str) -> str:
        if key not in PARTS:
            raise PromptError(f"unknown prompt part {key!r}")
        return resources.files("superagent").joinpath("prompts", f"{key}.md").read_text(encoding="utf-8").strip()

    def _override_path(self, key: str) -> Path | None:
        return self.override_dir / f"{key}.md" if self.override_dir else None

    def is_overridden(self, key: str) -> bool:
        path = self._override_path(key)
        return bool(path and path.is_file())

    def part(self, key: str) -> str:
        text = self.default(key)
        path = self._override_path(key)
        if path and path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            missing = [p for p in PLACEHOLDERS.get(key, ()) if p not in text]
            if missing:
                raise PromptError(f"override {path} must keep the placeholders {', '.join(missing)}")
        return text

    def validate(self) -> list[str]:
        """Check every override; returns the problems found, empty when all is well."""
        problems = []
        if self.override_dir and self.override_dir.is_dir():
            for path in sorted(self.override_dir.rglob("*.md")):
                key = path.relative_to(self.override_dir).with_suffix("").as_posix()
                if key not in PARTS:
                    problems.append(f"unknown prompt part {key!r} at {path}")
                    continue
                try:
                    self.part(key)
                except PromptError as exc:
                    problems.append(str(exc))
        return problems

    # -- assembly ----------------------------------------------------------------------------------------------

    def environment(self, variables: list[Variable]) -> str:
        lines = [f"- `{v.key}` = `{v.value}`: {v.description}" for v in variables] or ["- (none)"]
        return fill(self.part("environment"), variables="\n".join(lines))

    def policy(self, policy: Policy) -> str:
        rules = policy.enabled_rules()
        if not rules:
            return fill(self.part("policy-none"), version=policy.version, mode=policy.mode)
        lines = "\n".join(f"- {r.name} ({r.id}): {r.instruction}" for r in rules)
        return fill(self.part("policy"), version=policy.version, mode=policy.mode, rules=lines)

    def worker_system(self, variables: list[Variable], source_instructions: list[str] | None = None,
                      rules: list[Rule] | None = None) -> str:
        """Shared text, the worker role, the team sections, Source instructions, the environment and, for a
        proactive ticket, the rules it was accepted under."""
        sections = [self.part("shared"), self.part("worker")]
        sections += [self.part(key) for key in TEAM_PARTS]
        sections += [s for s in (source_instructions or []) if s.strip()]
        sections.append(self.environment(variables))
        if rules:
            listed = "\n".join(f"- {r.name} ({r.id}): {r.instruction}" for r in rules)
            sections.append(
                "# Proactive ticket\n\nNobody asked for this ticket directly; it was accepted under these team rules "
                "only. Stay within them, and follow their process and communication requirements where they "
                f"differ from the defaults:\n\n{listed}"
            )
        return "\n\n".join(sections)

    def assessor_system(self, policy: Policy) -> str:
        """Shared text, the assessor role, routing, the scope and communication sections, and the Policy (always last,
        so a Policy change leaves the cached prefix intact)."""
        return self._router_system("assessor", policy)

    def orchestrator_system(self, policy: Policy, source_instructions: list[str] | None = None) -> str:
        """Like the assessor's, with the orchestrator role, and the Source instructions so it knows how work is
        delivered. The Policy stays last."""
        return self._router_system("orchestrator", policy, source_instructions)

    def _router_system(self, role: str, policy: Policy, source_instructions: list[str] | None = None) -> str:
        sections = [self.part("shared"), self.part(role), self.part("routing"), self.part("team/scope"),
                    self.part("team/communication")]
        sections += [s for s in (source_instructions or []) if s.strip()]
        sections.append(self.policy(policy))
        return "\n\n".join(sections)
