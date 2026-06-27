"""Unit tests for acp.reports.templates — pure-string report rendering."""

from __future__ import annotations

from pathlib import Path

from acp.gitops.diff import DiffCapture
from acp.models import (
    AgentResult,
    CommandResult,
    Event,
    EventType,
    Recommendation,
    ReviewResult,
    RiskLevel,
    Task,
    TaskStatus,
)
from acp.reports.templates import render_failure_report, render_report
from acp.review.gates import GateOutcome, GateResult

# --- Fixtures --------------------------------------------------------------- #


def _task(
    status: TaskStatus = TaskStatus.PASSED,
    task_id: str = "task-001",
    repo_name: str = "demo-repo",
) -> Task:
    return Task(
        task_id=task_id,
        repo_name=repo_name,
        repo_path=Path("/tmp/repo"),
        base_branch="main",
        base_commit_sha="abc123",
        task_branch="task-001",
        worktree_path=Path("/tmp/worktree"),
        user_request="Fix the bug",
        status=status,
    )


def _cmd(exit_code: int = 0, skipped: bool = False) -> CommandResult:
    return CommandResult(
        command="pytest",
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout_path=Path("/tmp/stdout"),
        stderr_path=Path("/tmp/stderr"),
        duration_seconds=0.5,
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
        concerns=["watch the edge case"],
        summary="Looks good overall.",
        hard_block=hard_block,
    )


def _diff(changed_files: list[str] | None = None) -> DiffCapture:
    if changed_files is None:
        changed_files = ["src/main.py"]
    return DiffCapture(
        patch="diff --git a/src/main.py b/src/main.py\n",
        stat="1 file changed, 1 insertion(+)",
        changed_files=changed_files,
        insertions=1,
        deletions=0,
    )


def _agent(exit_code: int = 0) -> AgentResult:
    return AgentResult(
        agent_name="claude",
        exit_code=exit_code,
        stdout_path=Path("/tmp/agent_stdout.txt"),
        stderr_path=Path("/tmp/agent_stderr.txt"),
        summary="Agent completed the task.",
    )


def _event(event_type: EventType = EventType.TASK_CREATED, idx: int = 1) -> Event:
    return Event(
        event_id=f"evt_{idx:06d}",
        task_id="task-001",
        type=event_type,
        timestamp="2026-01-01T00:00:00Z",
        payload={},
        prev_hash="GENESIS",
        hash="abcdef0123456789",
    )


def _gate_result(outcome: GateOutcome = GateOutcome.PASSED) -> GateResult:
    return GateResult(
        outcome=outcome,
        reasons=["All final gates passed."],
        agent_exit_code=0,
        validation_commands_ran=1,
        validation_commands_failed=0,
        diff_is_empty=False,
        review_recommendation="merge",
        review_hard_block=False,
    )


# --- render_report ---------------------------------------------------------- #


def test_render_report_basic() -> None:
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        agent_result=_agent(),
    )
    assert "task-001" in body
    assert "passed" in body
    assert "demo-repo" in body


def test_render_report_with_gate_result() -> None:
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        agent_result=_agent(),
        gate_result=_gate_result(),
    )
    assert "Gate Summary" in body
    assert "Agent exit code" in body
    assert "All final gates passed." in body


def test_render_report_with_manifest_hash() -> None:
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        agent_result=_agent(),
        manifest_hash="deadbeefcafe",
    )
    assert "deadbeefcafe" in body
    assert "Evidence manifest hash" in body


def test_render_report_with_events() -> None:
    events = [
        _event(EventType.TASK_CREATED, 1),
        _event(EventType.AGENT_FINISHED, 2),
    ]
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        agent_result=_agent(),
        events=events,
    )
    assert "Event timeline" in body
    assert "evt_000001" in body
    assert "evt_000002" in body
    assert "task.created" in body
    assert "agent.finished" in body


def test_render_report_with_repair_history() -> None:
    repair_history = [
        {"attempt": 1, "prompt_path": "/tmp/artifacts/repair_prompt_1.txt"},
        {"attempt": 2, "prompt_path": "/tmp/artifacts/repair_prompt_2.txt"},
    ]
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        agent_result=_agent(),
        repair_history=repair_history,
    )
    assert "Repair attempts" in body
    assert "attempt 1" in body
    assert "attempt 2" in body


def test_render_report_with_agent_result() -> None:
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        agent_result=_agent(),
    )
    assert "claude" in body
    assert "Agent completed the task." in body


def test_render_report_empty_diff() -> None:
    body = render_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(changed_files=[]),
        agent_result=_agent(),
    )
    assert "0 file(s)" in body
    assert "_(none)_" in body


# --- render_failure_report -------------------------------------------------- #


def test_render_failure_report_basic() -> None:
    body = render_failure_report(
        task=_task(status=TaskStatus.FAILED),
        error="Worktree creation failed: branch already exists.",
    )
    assert "task-001" in body
    assert "failed" in body
    assert "Worktree creation failed" in body
    assert "## Failure" in body


def test_render_failure_report_with_events() -> None:
    events = [
        _event(EventType.TASK_CREATED, 1),
        _event(EventType.NODE_FAILED, 2),
    ]
    body = render_failure_report(
        task=_task(status=TaskStatus.FAILED),
        error="Node crashed.",
        events=events,
    )
    assert "Event timeline" in body
    assert "evt_000001" in body
    assert "evt_000002" in body
    assert "node.failed" in body


def test_render_failure_report_with_manifest_hash() -> None:
    body = render_failure_report(
        task=_task(status=TaskStatus.FAILED),
        error="Early failure.",
        manifest_hash="abc123def456",
    )
    assert "abc123def456" in body
    assert "Evidence manifest hash" in body
