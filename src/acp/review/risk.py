"""Risk engine — typed signal accumulation and risk-level combination.

A ``RiskSignal`` is one finding from one check: a category, a human-readable
concern, the risk level it imposes, and whether it's a hard block (forces
reject regardless of other signals). The ``RiskEngine`` collects signals
from every check and reduces them to the final ``ReviewResult`` fields.

Keeping the engine separate from the individual checks (path heuristics,
secret scanning, quantity thresholds) means the taxonomy is extensible: a
new check just appends a signal, and the engine handles combination and
hard-block logic uniformly. This is the M5 refactor target that the M1
reviewer grew into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from acp.models import Recommendation, RiskLevel


class RiskCategory(str, Enum):
    """Why a signal was raised. Surfaced in review.json for filtering/audit."""

    QUANTITY = "quantity"
    SECRET = "secret"
    ENV_FILE = "env_file"
    AUTH = "auth"
    DATABASE = "database"
    DEPENDENCY = "dependency"
    LOCKFILE = "lockfile"
    LARGE_FILE = "large_file"
    NETWORK = "network"
    PERMISSIONS = "permissions"
    TESTS_MISSING = "tests_missing"
    TESTS_FAILING = "tests_failing"
    EMPTY_DIFF = "empty_diff"
    NO_VALIDATION = "no_validation"


@dataclass
class RiskSignal:
    """One finding. ``hard_block=True`` forces REJECT and blocks auto-merge."""

    category: RiskCategory
    concern: str
    level: RiskLevel = RiskLevel.LOW
    hard_block: bool = False


@dataclass
class RiskEngine:
    """Accumulates signals and reduces them to the final risk + recommendation."""

    signals: list[RiskSignal] = field(default_factory=list)

    def add(
        self,
        category: RiskCategory,
        concern: str,
        *,
        level: RiskLevel = RiskLevel.LOW,
        hard_block: bool = False,
    ) -> None:
        self.signals.append(
            RiskSignal(category=category, concern=concern, level=level, hard_block=hard_block)
        )

    @property
    def hard_block(self) -> bool:
        return any(s.hard_block for s in self.signals)

    @property
    def level(self) -> RiskLevel:
        """The highest risk level across all signals (monotonic max)."""
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH]
        highest = RiskLevel.LOW
        for s in self.signals:
            if order.index(s.level) > order.index(highest):
                highest = s.level
        return highest

    @property
    def concerns(self) -> list[str]:
        return [s.concern for s in self.signals]

    def categories(self) -> list[str]:
        return [s.category.value for s in self.signals]

    def recommend(self, *, tests_pass: bool, require_human_approval: bool) -> Recommendation:
        """Map (risk, hard_block, test outcome, approval) → advisory recommendation.

        Always advisory — a human makes the final merge call — but a hard
        block always yields REJECT. When ``require_human_approval`` is True,
        the recommendation is never MERGE — the task must be reviewed by a
        human even if all signals are LOW and tests pass. This prevents
        autonomous mode from auto-merging changes that policy requires a
        human to approve.
        """
        if self.hard_block:
            return Recommendation.REJECT
        if self.level == RiskLevel.HIGH or not tests_pass:
            return Recommendation.REJECT
        if self.level == RiskLevel.MEDIUM:
            return Recommendation.REVISE
        # LOW risk + tests pass. If human approval is required, return
        # REVISE (needs review) instead of MERGE so the gate doesn't
        # auto-merge without a human decision.
        if require_human_approval:
            return Recommendation.REVISE
        return Recommendation.MERGE
