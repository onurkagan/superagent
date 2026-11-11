"""Policy: who may originate work, with what strength, and which proactive rules apply.

The gate here is deterministic. It runs before any model sees a message, so nothing a message says can widen what it
is allowed to start. See docs/002-authorization.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from superagent.core.model import Mandate, Strength, Trigger

AcceptMode = Literal["off", "observe", "auto"]
Cell = Literal["explicit", "inferred", "ignore"]

ACCEPT_MODES: tuple[str, ...] = ("off", "observe", "auto")
CELL_VALUES: tuple[str, ...] = ("explicit", "inferred", "ignore")


@dataclass(frozen=True)
class Rule:
    """A kind of proactive work the team allows, described in words the assessment and the worker read."""

    id: str
    name: str
    instruction: str
    enabled: bool = True


@dataclass(frozen=True)
class Admission:
    """What a human message becomes when it does not come from an allowed person addressing the agent.

    Cells for unaddressed messages apply only where the Source receives every message (``Trigger.observed``).
    """

    listed_unaddressed: Cell = "inferred"
    unlisted_addressed: Cell = "ignore"
    unlisted_unaddressed: Cell = "ignore"

    def cell(self, name: str) -> Cell:
        return getattr(self, name)


@dataclass(frozen=True)
class SourcePolicy:
    """A Source's part of the Policy, in that Source's own identities."""

    allowed: tuple[str, ...] = ()
    bots: tuple[str, ...] = ()
    admission: Admission = field(default_factory=Admission)

    def is_allowed(self, actor: str) -> bool:
        return actor.lower() in {a.lower() for a in self.allowed}

    def is_listed_bot(self, actor: str) -> bool:
        return actor.lower() in {b.lower() for b in self.bots}


@dataclass(frozen=True)
class GateResult:
    admitted: bool
    strength: Strength | None = None
    basis: str = ""
    cell: str = ""
    reason: str = ""

    @staticmethod
    def refuse(reason: str) -> GateResult:
        return GateResult(admitted=False, reason=reason)


class Policy:
    """Versioned rules. Any change to mode, rules or a Source part produces a new ``version``."""

    def __init__(
        self,
        mode: AcceptMode = "observe",
        rules: tuple[Rule, ...] = (),
        sources: dict[str, SourcePolicy] | None = None,
        eval_timeout_seconds: int = 600,
    ) -> None:
        if mode not in ACCEPT_MODES:
            raise ValueError(f"unknown accept mode {mode!r}; expected one of {', '.join(ACCEPT_MODES)}")
        self.mode: AcceptMode = mode
        self.rules = tuple(rules)
        self.sources = dict(sources or {})
        self.eval_timeout_seconds = eval_timeout_seconds
        self.version = self._compute_version()

    def _compute_version(self) -> str:
        canonical = {
            "mode": self.mode,
            "rules": sorted([r.id, r.name, r.instruction, r.enabled] for r in self.rules),
            "sources": {
                name: {
                    "allowed": sorted(a.lower() for a in sp.allowed),
                    "bots": sorted(b.lower() for b in sp.bots),
                    "admission": [sp.admission.listed_unaddressed, sp.admission.unlisted_addressed,
                                  sp.admission.unlisted_unaddressed],
                }
                for name, sp in sorted(self.sources.items())
            },
        }
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        return f"p-{digest[:10]}"

    # -- rules -------------------------------------------------------------------------------------------------

    def enabled_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def admits_inferred(self, rule_ids: list[str] | tuple[str, ...]) -> bool:
        """Whether an inferred candidate may be accepted on the rules its assessment matched."""
        enabled = {r.id for r in self.enabled_rules()}
        return self.mode == "auto" and bool(rule_ids) and all(r in enabled for r in rule_ids)

    # -- gate --------------------------------------------------------------------------------------------------

    def gate(self, trigger: Trigger, followup: bool = False) -> GateResult:
        """Decide whether ``trigger`` may become a candidate for new work, and with what strength.

        ``followup`` is true when the Trigger's Thread already carries a Ticket that a person did not stop.
        """
        result = self._gate(trigger, followup)
        if result.admitted and result.strength == "inferred" and self.mode == "off":
            return GateResult.refuse("inferred candidates are off")
        return result

    def _gate(self, trigger: Trigger, followup: bool) -> GateResult:
        sp = self.sources.get(trigger.source)
        if sp is None:
            return GateResult.refuse(f"no policy for source {trigger.source!r}")
        if not trigger.actor:
            return GateResult.refuse("event has no sender")
        if trigger.actor_is_bot:
            if sp.is_listed_bot(trigger.actor) and (trigger.addressed or trigger.observed):
                return GateResult(True, "inferred", "bot")
            return GateResult.refuse("bot is not on the bot list")

        listed = sp.is_allowed(trigger.actor)
        addressed = bool(trigger.addressed)
        if followup:
            if listed:
                return GateResult(True, "explicit", "followup")
            if addressed and sp.admission.unlisted_addressed == "explicit":
                return GateResult(True, "explicit", "admission", "unlisted_addressed")
            return GateResult(True, "inferred", "followup")
        if addressed and listed:
            return GateResult(True, "explicit", "allowlist")
        if addressed:
            cell_name = "unlisted_addressed"
        elif not trigger.observed:
            return GateResult.refuse("message does not address the agent")
        else:
            cell_name = "listed_unaddressed" if listed else "unlisted_unaddressed"
        cell = sp.admission.cell(cell_name)
        if cell == "ignore":
            return GateResult.refuse(f"admission table ignores {cell_name.replace('_', ' ')} messages")
        if cell == "explicit" and cell_name != "unlisted_addressed":
            cell = "inferred"  # only an addressed message can make an explicit request
        return GateResult(True, cell, "admission", cell_name)

    # -- revalidation ------------------------------------------------------------------------------------------

    def revalidate(self, mandate: Mandate, source_listens: bool = True) -> bool:
        """Re-check a Mandate's basis under this Policy before an ended Ticket resumes."""
        if not source_listens:
            return False
        sp = self.sources.get(mandate.source)
        if sp is None:
            return False
        if mandate.strength == "inferred":
            if mandate.rules and not self.admits_inferred(list(mandate.rules)):
                return False
            if not mandate.rules and self.mode != "auto":
                return False
        if mandate.basis == "followup":
            return mandate.strength == "explicit" or self.mode == "auto"
        if mandate.basis == "allowlist":
            return sp.is_allowed(mandate.requester)
        if mandate.basis == "bot":
            return sp.is_listed_bot(mandate.requester)
        if mandate.basis == "admission":
            listed = sp.is_allowed(mandate.requester)
            if mandate.cell == "unlisted_addressed" and listed:
                return True
            if mandate.cell in ("listed_unaddressed",) and not listed:
                return False
            cell = sp.admission.cell(mandate.cell) if mandate.cell else "ignore"
            rank = {"ignore": 0, "inferred": 1, "explicit": 2}
            return rank[cell] >= rank[mandate.strength]
        return sp.is_allowed(mandate.requester)
