"""Gate evaluator — the single place that decides final task outcome.

This is the v0.5 gate-correct logic. One function owns the decision:
a task may only be marked ``PASSED`` if all of the following hold:

* agent exit code was 0
* at least one non-skipped validation command actually ran
* all non-skipped commands passed (exit code 0)
* diff is non-empty (at least one changed file)
* review has no hard block
* review recommendation is ``merge``

The legacy ``compute_final_status()`` in ``models.py`` is now a thin wrapper
around this module. All new code should call ``evaluate_final_gates`` directly.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from acp.models import CommandResult, ReviewResult


class GateOutcome(StrEnum):
    """Three possible gate outcomes."""

    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class GateResult(BaseModel):
    """Structured result from evaluating all final gates."""

    outcome: GateOutcome
    reasons: list[str]
    agent_exit_code: int | None = None
    validation_commands_ran: int = 0
    validation_commands_failed: int = 0
    diff_is_empty: bool = True
    review_recommendation: str | None = None
    review_hard_block: bool = False


def evaluate_final_gates(
    *,
    agent_exit_code: int | None,
    command_results: list[CommandResult],
    review_result: ReviewResult | None = None,
    changed_files: list[str],
) -> GateResult:
    """Evaluate all final gates and return the structured result.

    This is the single source of truth for final outcome. Every caller that
    needs to decide PASSED / FAILED / NEEDS_REVIEW should use this function.

    When ``review_result`` is ``None`` the gate evaluator treats it as missing
    evidence — the outcome will be ``NEEDS_REVIEW`` unless there is already a
    hard failure.
    """
    reasons: list[str] = []

    non_skipped_commands = [r for r in command_results if not r.skipped]
    failed_commands = [r for r in non_skipped_commands if r.exit_code != 0]
    diff_is_empty = len(changed_files) == 0

    # --- Check each gate, accumulating reasons --------------------------- #

    if agent_exit_code is None:
        reasons.append("Agent exit code missing.")
        return GateResult(
            outcome=GateOutcome.FAILED,
            reasons=reasons,
            agent_exit_code=agent_exit_code,
            validation_commands_ran=len(non_skipped_commands),
            validation_commands_failed=len(failed_commands),
            diff_is_empty=diff_is_empty,
            review_recommendation=None,
            review_hard_block=False,
        )

    if agent_exit_code != 0:
        reasons.append(f"Agent exited nonzero: {agent_exit_code}.")
    if len(non_skipped_commands) == 0:
        reasons.append("No validation commands ran.")
    if failed_commands:
        reasons.append(f"{len(failed_commands)} validation command(s) failed.")
    if diff_is_empty:
        reasons.append("No files changed.")
    if review_result is None:
        reasons.append("Review result missing — cannot verify merge readiness.")
    else:
        if review_result.hard_block:
            reasons.append("Review hard block triggered.")
        if review_result.recommendation == "reject":
            reasons.append("Review recommendation is reject.")
        if review_result.recommendation == "revise":
            reasons.append("Review recommendation is revise.")

    # --- Determine outcome ----------------------------------------------- #

    # Hard failures
    if (
        agent_exit_code != 0
        or failed_commands
        or (
            review_result is not None
            and (review_result.hard_block or review_result.recommendation == "reject")
        )
    ):
        outcome = GateOutcome.FAILED
    # Completed, but not safe to call passed
    elif (
        len(non_skipped_commands) == 0
        or diff_is_empty
        or review_result is None
        or (review_result is not None and review_result.recommendation == "revise")
    ):
        outcome = GateOutcome.NEEDS_REVIEW
    else:
        outcome = GateOutcome.PASSED
        reasons.append("All final gates passed.")

    return GateResult(
        outcome=outcome,
        reasons=reasons,
        agent_exit_code=agent_exit_code,
        validation_commands_ran=len(non_skipped_commands),
        validation_commands_failed=len(failed_commands),
        diff_is_empty=diff_is_empty,
        review_recommendation=review_result.recommendation.value if review_result else None,
        review_hard_block=review_result.hard_block if review_result else False,
    )
