"""Unit tests for compute_final_status — the v0.5 gate-correct logic."""

from __future__ import annotations

from pathlib import Path

from acp.models import (
    CommandResult,
    Recommendation,
    ReviewResult,
    RiskLevel,
    TaskStatus,
    compute_final_status,
)


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


# --- All conditions met → PASSED ------------------------------------------- #


def test_all_green_passed() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.PASSED


def test_multiple_commands_all_pass_passed() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0), _cmd(0), _cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.PASSED


# --- Agent failures → FAILED ----------------------------------------------- #


def test_agent_nonzero_exit_failed() -> None:
    status = compute_final_status(
        agent_passed=False,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.FAILED


# --- No validation commands ran → NEEDS_REVIEW ----------------------------- #


def test_no_commands_ran_needs_review() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0, skipped=True)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.NEEDS_REVIEW


def test_empty_command_list_needs_review() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.NEEDS_REVIEW


# --- Failing commands → FAILED --------------------------------------------- #


def test_command_fails_failed() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(1)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.FAILED


def test_mixed_results_failed() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0), _cmd(1), _cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(),
    )
    assert status == TaskStatus.FAILED


# --- Empty diff → NEEDS_REVIEW --------------------------------------------- #


def test_empty_diff_needs_review() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=[],
        review=_review(),
    )
    assert status == TaskStatus.NEEDS_REVIEW


# --- Review checks → NEEDS_REVIEW / FAILED --------------------------------- #


def test_review_hard_block_failed() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(recommendation=Recommendation.REJECT, hard_block=True),
    )
    assert status == TaskStatus.FAILED


def test_review_revise_needs_review() -> None:
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(recommendation=Recommendation.REVISE),
    )
    assert status == TaskStatus.NEEDS_REVIEW


def test_review_reject_failed() -> None:
    """REJECT (even without hard_block) means FAILED per AGENTS.md rule 10."""
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=_review(recommendation=Recommendation.REJECT),
    )
    assert status == TaskStatus.FAILED


def test_none_review_with_good_commands_passed() -> None:
    """No review result at all — if everything else passes, it passes."""
    status = compute_final_status(
        agent_passed=True,
        command_results=[_cmd(0)],
        diff_changed_files=["src/main.py"],
        review=None,
    )
    assert status == TaskStatus.PASSED