"""Unit tests for the RiskEngine — typed signal accumulation and reduction."""

from __future__ import annotations

from acp.models import Recommendation, RiskLevel
from acp.review.risk import RiskCategory, RiskEngine


def test_empty_engine_is_low_risk() -> None:
    engine = RiskEngine()
    assert engine.level == RiskLevel.LOW
    assert not engine.hard_block
    assert engine.concerns == []


def test_single_signal_raises_level() -> None:
    engine = RiskEngine()
    engine.add(RiskCategory.QUANTITY, "too many files", level=RiskLevel.HIGH)
    assert engine.level == RiskLevel.HIGH


def test_highest_signal_wins() -> None:
    engine = RiskEngine()
    engine.add(RiskCategory.AUTH, "auth file", level=RiskLevel.LOW)
    engine.add(RiskCategory.DATABASE, "db migration", level=RiskLevel.HIGH)
    assert engine.level == RiskLevel.HIGH


def test_hard_block_propagates() -> None:
    engine = RiskEngine()
    assert not engine.hard_block
    engine.add(RiskCategory.SECRET, "leak detected", level=RiskLevel.HIGH, hard_block=True)
    assert engine.hard_block


def test_multiple_signals_collect_concerns() -> None:
    engine = RiskEngine()
    engine.add(RiskCategory.QUANTITY, "changed 30 files", level=RiskLevel.MEDIUM)
    engine.add(RiskCategory.AUTH, "auth file changed", level=RiskLevel.MEDIUM)
    assert len(engine.concerns) == 2
    assert "changed 30 files" in engine.concerns
    assert "auth file changed" in engine.concerns


def test_recommend_merge_low_pass() -> None:
    engine = RiskEngine()
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.MERGE


def test_recommend_revise_when_human_approval_required() -> None:
    """When require_human_approval=True and risk is LOW with passing tests,
    the recommendation is REVISE (needs human review) — not MERGE.

    This prevents autonomous mode from auto-merging changes that policy
    requires a human to approve. The engine now respects the
    require_human_approval flag at the engine level.
    """
    engine = RiskEngine()
    rec = engine.recommend(tests_pass=True, require_human_approval=True)
    assert rec == Recommendation.REVISE


def test_recommend_reject_on_hard_block() -> None:
    engine = RiskEngine()
    engine.add(RiskCategory.SECRET, "secret", level=RiskLevel.HIGH, hard_block=True)
    rec = engine.recommend(tests_pass=True, require_human_approval=True)
    assert rec == Recommendation.REJECT


def test_recommend_reject_on_high_risk() -> None:
    engine = RiskEngine()
    engine.add(RiskCategory.QUANTITY, "massive diff", level=RiskLevel.HIGH)
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.REJECT


def test_recommend_revise_on_failing_tests() -> None:
    """Tests failing → REJECT (can't merge with red tests)."""
    engine = RiskEngine()
    rec = engine.recommend(tests_pass=False, require_human_approval=False)
    assert rec == Recommendation.REJECT


def test_recommend_revise_on_medium_risk() -> None:
    engine = RiskEngine()
    engine.add(RiskCategory.AUTH, "auth file", level=RiskLevel.MEDIUM)
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.REVISE


def test_risk_category_values() -> None:
    """All RiskCategory values are present and unique."""
    cats = {c.value for c in RiskCategory}
    assert "quantity" in cats
    assert "secret" in cats
    assert "empty_diff" in cats
    assert "tests_failing" in cats
