"""Unit tests for acp.reports.writer — report persistence + rerendering."""

from __future__ import annotations

import json
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
from acp.reports.writer import (
    rerender_report_from_run,
    write_failure_report,
    write_report,
)
from acp.review.gates import GateOutcome, GateResult

# --- Fixtures --------------------------------------------------------------- #


def _task(
    status: TaskStatus = TaskStatus.PASSED,
    task_id: str = "task-001",
) -> Task:
    return Task(
        task_id=task_id,
        repo_name="demo-repo",
        repo_path=Path("/tmp/repo"),
        base_branch="main",
        base_commit_sha="abc123",
        task_branch="task-001",
        worktree_path=Path("/tmp/worktree"),
        user_request="Fix the bug",
        status=status,
    )


def _cmd(exit_code: int = 0) -> CommandResult:
    return CommandResult(
        command="pytest",
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout_path=Path("/tmp/stdout"),
        stderr_path=Path("/tmp/stderr"),
        duration_seconds=0.5,
    )


def _review() -> ReviewResult:
    return ReviewResult(
        risk=RiskLevel.LOW,
        recommendation=Recommendation.MERGE,
        changed_files=["src/main.py"],
        summary="Looks good.",
    )


def _diff() -> DiffCapture:
    return DiffCapture(
        patch="diff --git a/src/main.py b/src/main.py\n",
        stat="1 file changed, 1 insertion(+)",
        changed_files=["src/main.py"],
        insertions=1,
        deletions=0,
    )


def _agent() -> AgentResult:
    return AgentResult(
        agent_name="claude",
        exit_code=0,
        stdout_path=Path("/tmp/agent_stdout.txt"),
        stderr_path=Path("/tmp/agent_stderr.txt"),
        summary="Done.",
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


def _gate_result() -> GateResult:
    return GateResult(
        outcome=GateOutcome.PASSED,
        reasons=["All final gates passed."],
        agent_exit_code=0,
        validation_commands_ran=1,
        validation_commands_failed=0,
        diff_is_empty=False,
        review_recommendation="merge",
        review_hard_block=False,
    )


# --- write_report ----------------------------------------------------------- #


def test_write_report_creates_file(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    report_path = write_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        artifact_dir=artifact_dir,
        agent_result=_agent(),
        gate_result=_gate_result(),
        manifest_hash="deadbeef",
    )
    assert report_path == artifact_dir / "final_report.md"
    assert report_path.is_file()
    body = report_path.read_text()
    assert "task-001" in body
    assert "Gate Summary" in body
    assert "deadbeef" in body


def test_write_report_creates_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "nested" / "artifacts"
    assert not artifact_dir.exists()
    report_path = write_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        artifact_dir=artifact_dir,
        agent_result=_agent(),
    )
    assert artifact_dir.is_dir()
    assert report_path.is_file()


# --- write_failure_report --------------------------------------------------- #


def test_write_failure_report_creates_file(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    report_path = write_failure_report(
        task=_task(status=TaskStatus.FAILED),
        error="Worktree creation failed.",
        artifact_dir=artifact_dir,
    )
    assert report_path == artifact_dir / "final_report.md"
    assert report_path.is_file()
    body = report_path.read_text()
    assert "## Failure" in body
    assert "Worktree creation failed." in body


def test_write_failure_report_creates_dir(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "deep" / "nested" / "artifacts"
    assert not artifact_dir.exists()
    report_path = write_failure_report(
        task=_task(status=TaskStatus.FAILED),
        error="Early failure.",
        artifact_dir=artifact_dir,
    )
    assert artifact_dir.is_dir()
    assert report_path.is_file()


# --- rerender_report_from_run ----------------------------------------------- #


def test_rerender_report_no_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "task.json").write_text(_task().model_dump_json())
    assert rerender_report_from_run(run_dir) is None


def test_rerender_report_no_task_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "final_report.md").write_text("# Task report: task-001\n")
    assert rerender_report_from_run(run_dir) is None


def test_rerender_report_failure_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)

    # Write an initial failure report.
    write_failure_report(
        task=_task(status=TaskStatus.FAILED),
        error="Original failure.",
        artifact_dir=artifacts,
        events=[_event(EventType.TASK_CREATED, 1)],
    )

    # Write task.json, events.jsonl, and manifest.
    (run_dir / "task.json").write_text(_task(status=TaskStatus.FAILED).model_dump_json())
    events = [_event(EventType.TASK_CREATED, 1), _event(EventType.NODE_FAILED, 2)]
    (run_dir / "events.jsonl").write_text("\n".join(e.model_dump_json() for e in events) + "\n")
    (run_dir / "evidence_manifest.json").write_text(json.dumps({"manifest_hash": "newhash123"}))

    result = rerender_report_from_run(run_dir)
    assert result is not None
    body = result.read_text()
    assert "## Failure" in body
    assert "newhash123" in body
    assert "evt_000002" in body
    assert "node.failed" in body


def test_rerender_report_full_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)

    # Write an initial full report with an old manifest hash + event timeline.
    write_report(
        task=_task(),
        command_results=[_cmd()],
        review=_review(),
        diff=_diff(),
        artifact_dir=artifacts,
        agent_result=_agent(),
        gate_result=_gate_result(),
        manifest_hash="deadbeef00",
        events=[_event(EventType.TASK_CREATED, 1)],
    )

    # Write task.json, updated events.jsonl, and updated manifest.
    (run_dir / "task.json").write_text(_task().model_dump_json())
    events = [
        _event(EventType.TASK_CREATED, 1),
        _event(EventType.HUMAN_APPROVED, 2),
    ]
    (run_dir / "events.jsonl").write_text("\n".join(e.model_dump_json() for e in events) + "\n")
    (run_dir / "evidence_manifest.json").write_text(json.dumps({"manifest_hash": "newhash999"}))

    result = rerender_report_from_run(run_dir)
    assert result is not None
    body = result.read_text()
    assert "newhash999" in body
    assert "deadbeef00" not in body
    assert "evt_000002" in body
    assert "human.approved" in body
