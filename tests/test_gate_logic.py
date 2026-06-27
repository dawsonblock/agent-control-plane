"""Unit tests for evaluate_final_gates — the v0.5 gate-correct logic.

v0.7.6: Migrated from the deprecated ``compute_final_status`` wrapper to
``evaluate_final_gates`` directly, per the tech-debt cleanup plan. The
outcome-to-TaskStatus mapping that the wrapper performed is replicated
here via a small local helper.
"""

from __future__ import annotations

from pathlib import Path

from acp.models import CommandResult, Recommendation, ReviewResult, RiskLevel, TaskStatus
from acp.review.gates import GateOutcome, evaluate_final_gates


def _cmd(exit_code: int = 0, skipped: bool = False) -> CommandResult:
    return CommandResult(
        command="echo test",
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout_path=Path("/tmp/stdout"),
        stderr_path=Path("/tmp/stderr"),
        duration_seconds=0.1,
        skipped=skipped,
    )


def _review(
    recommendation: Recommendation = Recommendation.MERGE,
    hard_block: bool = False,
) -> ReviewResult:
    return ReviewResult(
        risk=RiskLevel.LOW,
        recommendation=recommendation,
        changed_files=["src/main.py"],
        summary="ok",
        hard_block=hard_block,
    )


def _status(*, agent_passed: bool, command_results, diff_changed_files, review) -> TaskStatus:
    """Replicate the compute_final_status wrapper: evaluate gates → TaskStatus."""
    result = evaluate_final_gates(
        agent_exit_code=0 if agent_passed else 1,
        command_results=command_results,
        review_result=review,
        changed_files=diff_changed_files,
    )
    if result.outcome == GateOutcome.PASSED:
        return TaskStatus.PASSED
    if result.outcome == GateOutcome.NEEDS_REVIEW:
        return TaskStatus.NEEDS_REVIEW
    return TaskStatus.FAILED


# --- All conditions met → PASSED ------------------------------------------- #


def test_all_green_passed() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.PASSED


def test_multiple_commands_all_pass_passed() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0), _cmd(0), _cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.PASSED


# --- Agent failures → FAILED ----------------------------------------------- #


def test_agent_nonzero_exit_failed() -> None:
    status = _status(
        agent_passed=False,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.FAILED


# --- No validation commands ran → NEEDS_REVIEW ----------------------------- #


def test_no_commands_ran_needs_review() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0, skipped=True)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.NEEDS_REVIEW


def test_empty_command_list_needs_review() -> None:
    status = _status(
        agent_passed=True,
        command_results=[],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.NEEDS_REVIEW


# --- Failing commands → FAILED --------------------------------------------- #


def test_command_fails_failed() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(1)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.FAILED


def test_mixed_results_failed() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0), _cmd(1), _cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.FAILED


# --- Empty diff → NEEDS_REVIEW --------------------------------------------- #


def test_empty_diff_needs_review() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=[],
        review=_review(),
    )
    assert status == TaskStatus.NEEDS_REVIEW


# --- Review checks → NEEDS_REVIEW / FAILED --------------------------------- #


def test_review_hard_block_failed() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(recommendation=Recommendation.REJECT, hard_block=True),
    )
    assert status == TaskStatus.FAILED


def test_review_revise_needs_review() -> None:
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(recommendation=Recommendation.REVISE),
    )
    assert status == TaskStatus.NEEDS_REVIEW


def test_review_reject_failed() -> None:
    """REJECT (even without hard_block) means FAILED per AGENTS.md rule 10."""
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(recommendation=Recommendation.REJECT),
    )
    assert status == TaskStatus.FAILED


def test_none_review_with_good_commands_needs_review() -> None:
    """No review result — missing review means NEEDS_REVIEW, not PASSED.

    A missing review is incomplete evidence. The system must not mark a task
    PASSED without a review confirming merge readiness.
    """
    status = _status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=None,
    )
    assert status == TaskStatus.NEEDS_REVIEW, (
        f"expected NEEDS_REVIEW for missing review, got {status}"
    )
