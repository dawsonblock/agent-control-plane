"""Tests for the approval workflow — vault note approval + CLI commands.

Covers:
  - approve_vault_note: flips frontmatter, adds audit trail
  - reject_vault_note: archives note, adds audit trail
  - can_approve: eligibility checks
  - Safety: can't approve twice, can't reject after approve, can't reject twice
  - `acp approve` CLI: approves a passed task, writes event, updates status
  - `acp approve` CLI: rejects ineligible status (failed)
  - `acp approve` CLI: rejects already-approved note
  - `acp reject` CLI: rejects a task, writes event, updates status
  - `acp reject` CLI: can't reject after approval
  - `acp list` CLI: lists tasks, filters by status
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from acp.cli import app
from acp.events import EventWriter, verify_event_chain
from acp.models import EventType, Task, TaskStatus
from acp.store import TaskStore
from acp.vault.approval import approve_vault_note, can_approve, reject_vault_note
from acp.vault.frontmatter import build_frontmatter, parse_frontmatter
from acp.gitops.diff import DiffCapture
from acp.models import Recommendation, ReviewResult, RiskLevel


runner = CliRunner()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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


def _write_vault_note(vault_root: Path, task: Task, review: ReviewResult, diff: DiffCapture) -> Path:
    """Write a vault note with frontmatter + body."""
    from acp.vault.obsidian_writer import write_vault_note
    return write_vault_note(
        report_body="# Task report\n\nTest body.",
        task=task,
        review=review,
        diff=diff,
        vault_root=vault_root,
    )


def _setup_run(runs_root: Path, vault_root: Path, task_id: str = "task_20260624_0001",
               status: TaskStatus = TaskStatus.PASSED) -> tuple[Task, Path]:
    """Set up a run directory with task.json + vault note."""
    store = TaskStore(runs_root=runs_root)
    task = _make_task(task_id, status)
    run_dir = store.run_dir(task_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    store.artifacts_dir(task_id).mkdir(parents=True, exist_ok=True)
    store.save(task)
    note_path = _write_vault_note(vault_root, task, _make_review(), _make_diff())
    return task, note_path


# --------------------------------------------------------------------------- #
# Unit tests: approve_vault_note
# --------------------------------------------------------------------------- #


def test_approve_vault_note_flips_frontmatter(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    fm = approve_vault_note(note_path, approver="alice")
    assert fm.approved is True
    assert fm.memory_status == "active"

    # Re-read and verify.
    content = note_path.read_text()
    fm2, body = parse_frontmatter(content)
    assert fm2.approved is True
    assert fm2.memory_status == "active"


def test_approve_vault_note_adds_audit_trail(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    approve_vault_note(note_path, approver="alice", now=datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc))

    content = note_path.read_text()
    stripped = content.split("---")[1]
    data = yaml.safe_load(stripped)
    assert "audit_trail" in data
    assert len(data["audit_trail"]) == 1
    assert data["audit_trail"][0]["action"] == "approved"
    assert data["audit_trail"][0]["actor"] == "alice"


def test_approve_vault_note_already_approved_raises(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    approve_vault_note(note_path, approver="alice")
    with pytest.raises(PermissionError, match="already approved"):
        approve_vault_note(note_path, approver="bob")


def test_approve_vault_note_nonexistent_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        approve_vault_note(tmp_path / "nonexistent.md")


# --------------------------------------------------------------------------- #
# Unit tests: reject_vault_note
# --------------------------------------------------------------------------- #


def test_reject_vault_note_archives(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    fm = reject_vault_note(note_path, rejecter="bob")
    assert fm.approved is False
    assert fm.memory_status == "archived"


def test_reject_vault_note_adds_audit_trail(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    reject_vault_note(note_path, rejecter="bob")
    content = note_path.read_text()
    stripped = content.split("---")[1]
    data = yaml.safe_load(stripped)
    assert data["audit_trail"][0]["action"] == "rejected"
    assert data["audit_trail"][0]["actor"] == "bob"


def test_reject_after_approve_raises(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    approve_vault_note(note_path, approver="alice")
    with pytest.raises(PermissionError, match="already approved"):
        reject_vault_note(note_path, rejecter="bob")


def test_reject_twice_raises(tmp_path: Path):
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(tmp_path / "runs", vault_root)

    reject_vault_note(note_path, rejecter="bob")
    with pytest.raises(PermissionError, match="already archived"):
        reject_vault_note(note_path, rejecter="bob")


# --------------------------------------------------------------------------- #
# Unit tests: can_approve
# --------------------------------------------------------------------------- #


def test_can_approve_passed():
    assert can_approve(TaskStatus.PASSED) is True


def test_can_approve_needs_review():
    assert can_approve(TaskStatus.NEEDS_REVIEW) is True


def test_can_approve_failed():
    assert can_approve(TaskStatus.FAILED) is False


def test_can_approve_approved():
    assert can_approve(TaskStatus.APPROVED) is False


def test_can_approve_archived():
    assert can_approve(TaskStatus.ARCHIVED) is False


# --------------------------------------------------------------------------- #
# CLI tests: acp approve
# --------------------------------------------------------------------------- #


def test_cli_approve_passed_task(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root)

    r = runner.invoke(app, [
        "approve", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
        "--approver", "alice@example.com",
    ])
    assert r.exit_code == 0, f"approve failed: {r.output}"
    assert "approved by alice@example.com" in r.output
    assert "human.approved" in r.output

    # Verify task status updated.
    store = TaskStore(runs_root=runs_root)
    updated = store.load(task.task_id)
    assert updated.status == TaskStatus.APPROVED

    # Verify event written.
    events = EventWriter(task.task_id, store.run_dir(task.task_id))
    all_events = events.read_all()
    approved_events = [e for e in all_events if e.type == EventType.HUMAN_APPROVED]
    assert len(approved_events) == 1
    assert approved_events[0].payload["approver"] == "alice@example.com"

    # Verify event chain still valid.
    assert verify_event_chain(all_events) is True

    # Verify vault note frontmatter.
    fm, _ = parse_frontmatter(note_path.read_text())
    assert fm.approved is True
    assert fm.memory_status == "active"


def test_cli_approve_failed_task_rejected(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root, status=TaskStatus.FAILED)

    r = runner.invoke(app, [
        "approve", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r.exit_code == 1
    assert "only 'passed' or 'needs_review'" in r.output


def test_cli_approve_already_approved(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root)

    # First approval succeeds.
    r1 = runner.invoke(app, [
        "approve", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r1.exit_code == 0

    # Second approval fails — caught by the task status check (already APPROVED).
    r2 = runner.invoke(app, [
        "approve", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r2.exit_code == 1
    assert "only 'passed' or 'needs_review'" in r2.output


def test_cli_approve_nonexistent_task(tmp_path: Path):
    r = runner.invoke(app, [
        "approve", "--task", "task_20260624_9999",
        "--runs-root", str(tmp_path / "runs"),
        "--vault-root", str(tmp_path / "vault"),
    ])
    assert r.exit_code == 1
    assert "not found" in r.output


def test_cli_approve_invalid_task_id_rejected(tmp_path: Path):
    """Invalid (path-shaped) task ids are rejected before any filesystem access."""
    r = runner.invoke(app, [
        "approve", "--task", "../etc/passwd",
        "--runs-root", str(tmp_path / "runs"),
        "--vault-root", str(tmp_path / "vault"),
    ])
    assert r.exit_code == 1
    assert "invalid task id" in r.output


# --------------------------------------------------------------------------- #
# CLI tests: acp reject
# --------------------------------------------------------------------------- #


def test_cli_reject_task(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root)

    r = runner.invoke(app, [
        "reject", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
        "--rejecter", "bob@example.com",
        "--reason", "too risky",
    ])
    assert r.exit_code == 0, f"reject failed: {r.output}"
    assert "rejected by bob@example.com" in r.output
    assert "human.rejected" in r.output

    # Verify task status updated.
    store = TaskStore(runs_root=runs_root)
    updated = store.load(task.task_id)
    assert updated.status == TaskStatus.ARCHIVED

    # Verify event written.
    events = EventWriter(task.task_id, store.run_dir(task.task_id))
    all_events = events.read_all()
    rejected_events = [e for e in all_events if e.type == EventType.HUMAN_REJECTED]
    assert len(rejected_events) == 1
    assert rejected_events[0].payload["rejecter"] == "bob@example.com"
    assert rejected_events[0].payload["reason"] == "too risky"


def test_cli_reject_after_approve_fails(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"
    task, note_path = _setup_run(runs_root, vault_root)

    # Approve first.
    r1 = runner.invoke(app, [
        "approve", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r1.exit_code == 0

    # Reject should fail.
    r2 = runner.invoke(app, [
        "reject", "--task", task.task_id,
        "--runs-root", str(runs_root),
        "--vault-root", str(vault_root),
    ])
    assert r2.exit_code == 1
    assert "already approved" in r2.output


# --------------------------------------------------------------------------- #
# CLI tests: acp list
# --------------------------------------------------------------------------- #


def test_cli_list_tasks(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"

    # Create three tasks with different statuses.
    _setup_run(runs_root, vault_root, "task_20260624_0001", TaskStatus.PASSED)
    _setup_run(runs_root, vault_root, "task_20260624_0002", TaskStatus.FAILED)
    _setup_run(runs_root, vault_root, "task_20260624_0003", TaskStatus.NEEDS_REVIEW)

    r = runner.invoke(app, ["list", "--runs-root", str(runs_root)])
    assert r.exit_code == 0
    assert "task_20260624_0001" in r.output
    assert "task_20260624_0002" in r.output
    assert "task_20260624_0003" in r.output
    assert "3 total" in r.output


def test_cli_list_tasks_filter_by_status(tmp_path: Path):
    runs_root = tmp_path / "runs"
    vault_root = tmp_path / "vault"

    _setup_run(runs_root, vault_root, "task_20260624_0001", TaskStatus.PASSED)
    _setup_run(runs_root, vault_root, "task_20260624_0002", TaskStatus.FAILED)

    r = runner.invoke(app, ["list", "--runs-root", str(runs_root), "--status", "passed"])
    assert r.exit_code == 0
    assert "task_20260624_0001" in r.output
    assert "task_20260624_0002" not in r.output
    assert "status=passed" in r.output


def test_cli_list_tasks_empty(tmp_path: Path):
    r = runner.invoke(app, ["list", "--runs-root", str(tmp_path / "nonexistent")])
    assert r.exit_code == 0
    assert "No runs directory" in r.output
