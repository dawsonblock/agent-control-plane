"""Tests for bug fixes and edge cases found during code audit.

Covers:
  - audit_trail preservation across re-parses (Frontmatter model fix)
  - acp verify with no event log (NameError fix)
  - acp verify with no manifest (pre-v0.5.5 run)
  - acp events with --limit
  - acp approve on a needs_review task (not just passed)
  - acp reject on a failed task
  - acp list with no runs directory
  - Frontmatter audit_trail field round-trips through YAML
  - approve_vault_note preserves existing audit_trail entries
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from acp.cli import app
from acp.events import EventWriter
from acp.gitops.diff import DiffCapture
from acp.models import (
    EventType,
    Recommendation,
    ReviewResult,
    RiskLevel,
    Task,
    TaskStatus,
)
from acp.store import TaskStore
from acp.vault.approval import approve_vault_note, reject_vault_note
from acp.vault.frontmatter import build_frontmatter, parse_frontmatter
from acp.vault.obsidian_writer import write_vault_note


runner = CliRunner()


def _make_task(task_id: str = "task_20260624_0001", status: TaskStatus = TaskStatus.PASSED) -> Task:
    return Task(
        task_id=task_id,
        repo_name="demo",
        repo_path=Path("/tmp/demo"),
        base_branch="main",
        task_branch=f"agent/{task_id}",
        worktree_path=Path(f"/tmp/runs/{task_id}/worktree"),
        user_request="test task",
        status=status,
    )


def _make_review() -> ReviewResult:
    return ReviewResult(
        risk=RiskLevel.LOW,
        recommendation=Recommendation.MERGE,
        summary="looks good",
    )


def _make_diff() -> DiffCapture:
    return DiffCapture(patch="", stat="", changed_files=["a.py"], insertions=1, deletions=0)


def _setup_run(runs_root: Path, vault_root: Path, task_id: str = "task_20260624_0001",
               status: TaskStatus = TaskStatus.PASSED) -> tuple[Task, Path]:
    store = TaskStore(runs_root=runs_root)
    task = _make_task(task_id, status)
    run_dir = store.run_dir(task_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    store.artifacts_dir(task_id).mkdir(parents=True, exist_ok=True)
    store.save(task)
    note_path = write_vault_note(
        report_body="# Task report\n\nTest body.",
        task=task,
        review=_make_review(),
        diff=_make_diff(),
        vault_root=vault_root,
    )
    return task, note_path


# --------------------------------------------------------------------------- #
# Bug fix: audit_trail preservation in Frontmatter model
# --------------------------------------------------------------------------- #


def test_frontmatter_audit_trail_field_exists():
    """Frontmatter model should have an audit_trail field (bug fix)."""
    from acp.vault.frontmatter import Frontmatter
    fm = Frontmatter(type="task_report")
    assert hasattr(fm, "audit_trail")
    assert fm.audit_trail == []


def test_frontmatter_audit_trail_round_trips():
    """audit_trail should survive a parse → dump → parse cycle."""
    task = _make_task()
    review = _make_review()
    diff = _make_diff()
    fm_str = build_frontmatter(task=task, review=review, diff=diff)

    # Manually add an audit_trail to the YAML.
    data = yaml.safe_load(fm_str.removeprefix("---").removesuffix("---").strip())
    data["audit_trail"] = [
        {"action": "approved", "actor": "alice", "timestamp": "2026-06-24T12:00:00Z"},
    ]
    new_fm_str = f"---\n{yaml.safe_dump(data, sort_keys=False).strip()}\n---\n\nbody"

    fm, body = parse_frontmatter(new_fm_str)
    assert len(fm.audit_trail) == 1
    assert fm.audit_trail[0]["action"] == "approved"
    assert fm.audit_trail[0]["actor"] == "alice"


def test_approve_then_reject_preserves_audit_trail(tmp_path: Path):
    """If a note is approved and then someone tries to reject, the audit
    trail from the approval should be preserved in the error path.

    Actually, reject after approve raises PermissionError. But the audit
    trail should still be intact in the file (the approve wrote it).
    """
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    # Approve — writes audit_trail.
    approve_vault_note(note_path, approver="alice")

    # Try to reject — should fail.
    import pytest
    with pytest.raises(PermissionError):
        reject_vault_note(note_path, rejecter="bob")

    # The audit_trail from the approve should still be in the file.
    fm, _ = parse_frontmatter(note_path.read_text())
    assert len(fm.audit_trail) == 1
    assert fm.audit_trail[0]["action"] == "approved"
    assert fm.audit_trail[0]["actor"] == "alice"


# --------------------------------------------------------------------------- #
# Bug fix: acp verify with no event log (NameError fix)
# --------------------------------------------------------------------------- #


def test_cli_verify_no_event_log(tmp_path: Path):
    """`acp verify` on a run with no event log should not crash (NameError fix)."""
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    run_dir = runs_root / task_id
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(_make_task(task_id).model_dump_json(indent=2))

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(runs_root),
    ])
    assert r.exit_code == 1
    assert "event log not found" in r.output


def test_cli_verify_no_manifest(tmp_path: Path):
    """`acp verify` on a run with no manifest should warn, not fail."""
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    run_dir = runs_root / task_id
    run_dir.mkdir(parents=True)

    # Write task.json.
    (run_dir / "task.json").write_text(_make_task(task_id).model_dump_json(indent=2))

    # Write a valid event log (but no manifest).
    events = EventWriter(task_id, run_dir)
    events.write(EventType.TASK_CREATED, {"request": "test"})
    events.write(EventType.TASK_COMPLETED, {"status": "passed"})

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(runs_root),
    ])
    # Should pass (manifest is optional for pre-v0.5.5 runs).
    assert r.exit_code == 0
    assert "event chain valid" in r.output
    assert "manifest not found" in r.output


def test_cli_verify_with_public_key_no_event_log(tmp_path: Path):
    """`acp verify --public-key` with no event log should not crash."""
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    run_dir = runs_root / task_id
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(_make_task(task_id).model_dump_json(indent=2))

    # Create a dummy public key file.
    pub_key = tmp_path / "pubkey.bin"
    pub_key.write_bytes(b"\x00" * 32)

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(runs_root),
        "--public-key", str(pub_key),
    ])
    assert r.exit_code == 1
    assert "event log not found" in r.output


# --------------------------------------------------------------------------- #
# Edge case: acp events with --limit
# --------------------------------------------------------------------------- #


def test_cli_events_with_limit(tmp_path: Path):
    """`acp events --limit` should cap the output."""
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    run_dir = runs_root / task_id
    run_dir.mkdir(parents=True)

    events = EventWriter(task_id, run_dir)
    for i in range(10):
        events.write(EventType.TASK_CREATED, {"i": i})

    r = runner.invoke(app, [
        "events", "--task", task_id,
        "--runs-root", str(runs_root),
        "--limit", "3",
    ])
    assert r.exit_code == 0
    # Should only show 3 events.
    assert "evt_000001" in r.output
    assert "evt_000002" in r.output
    assert "evt_000003" in r.output
    assert "evt_000004" not in r.output


# --------------------------------------------------------------------------- #
# Edge case: acp approve on needs_review task
# --------------------------------------------------------------------------- #


def test_cli_approve_needs_review_task(tmp_path: Path):
    """`acp approve` on a NEEDS_REVIEW task should succeed."""
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root, status=TaskStatus.NEEDS_REVIEW)

    r = runner.invoke(app, [
        "approve", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
        "--approver", "alice",
    ])
    assert r.exit_code == 0
    assert "approved by alice" in r.output

    store = TaskStore(runs_root=runs_root)
    updated = store.load(task.task_id)
    assert updated.status == TaskStatus.APPROVED


# --------------------------------------------------------------------------- #
# Edge case: acp reject on a failed task
# --------------------------------------------------------------------------- #


def test_cli_reject_failed_task(tmp_path: Path):
    """`acp reject` on a FAILED task should succeed (archiving it)."""
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root, status=TaskStatus.FAILED)

    r = runner.invoke(app, [
        "reject", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
        "--rejecter", "bob",
        "--reason", "tests failed",
    ])
    assert r.exit_code == 0
    assert "rejected by bob" in r.output

    store = TaskStore(runs_root=runs_root)
    updated = store.load(task.task_id)
    assert updated.status == TaskStatus.REJECTED


# --------------------------------------------------------------------------- #
# Edge case: acp list with no runs directory
# --------------------------------------------------------------------------- #


def test_cli_list_no_runs_directory(tmp_path: Path):
    """`acp list` with a non-existent runs directory should not crash."""
    r = runner.invoke(app, ["list", "--runs-root", str(tmp_path / "nonexistent")])
    assert r.exit_code == 0
    assert "No runs directory" in r.output


def test_cli_list_empty_runs_directory(tmp_path: Path):
    """`acp list` with an empty runs directory should show no tasks."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()

    r = runner.invoke(app, ["list", "--runs-root", str(runs_root)])
    assert r.exit_code == 0
    assert "No tasks found" in r.output


# --------------------------------------------------------------------------- #
# Edge case: reject on already-archived task
# --------------------------------------------------------------------------- #


def test_cli_reject_already_archived(tmp_path: Path):
    """`acp reject` on an already-archived task should fail."""
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root)

    # First reject succeeds.
    r1 = runner.invoke(app, [
        "reject", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r1.exit_code == 0

    # Second reject fails.
    r2 = runner.invoke(app, [
        "reject", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r2.exit_code == 1
    assert "already rejected" in r2.output


# --------------------------------------------------------------------------- #
# Edge case: approve with no vault note
# --------------------------------------------------------------------------- #


def test_cli_approve_no_vault_note(tmp_path: Path):
    """`acp approve` when the vault note doesn't exist should fail clearly."""
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    run_dir = runs_root / task_id
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(_make_task(task_id).model_dump_json(indent=2))

    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(tmp_path / "vault"),
    ])
    assert r.exit_code == 1
    assert "vault note not found" in r.output


# --------------------------------------------------------------------------- #
# Edge case: verify with empty event log
# --------------------------------------------------------------------------- #


def test_cli_verify_empty_event_log(tmp_path: Path):
    """`acp verify` on a run with an empty event log should report it."""
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    run_dir = runs_root / task_id
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(_make_task(task_id).model_dump_json(indent=2))
    (run_dir / "events.jsonl").write_text("")  # empty file

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(runs_root),
    ])
    assert r.exit_code == 1
    assert "event log is empty" in r.output
