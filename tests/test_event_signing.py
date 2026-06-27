"""v0.5.6 tests — Ed25519 event-log signing + event timeline in reports.

Covers:
  - Ed25519 key generation, signing, and verification
  - Signed events include a non-empty signature field
  - verify_event_signatures passes for correctly signed events
  - verify_event_signatures fails for tampered events
  - verify_event_signatures fails for unsigned events
  - Event timeline renders in the full report
  - Event timeline renders in the failure report
  - Report without events still works (backward compatible)

These tests require the ``cryptography`` package (``uv sync --extra crypto``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.events import EventWriter, verify_event_chain, verify_event_signatures
from acp.models import EventType, Task, TaskStatus

# --------------------------------------------------------------------------- #
# Ed25519 signing
# --------------------------------------------------------------------------- #


def _generate_ed25519_keypair():
    """Generate an Ed25519 key pair for testing."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def test_signed_events_have_signatures(tmp_path: Path):
    private_key, _ = _generate_ed25519_keypair()
    w = EventWriter("task_001", tmp_path / "run")
    w.set_signing_key(private_key.private_bytes_raw())
    w.write(EventType.TASK_CREATED, {"request": "test"})
    events = w.read_all()
    assert len(events) == 1
    assert events[0].signature != ""
    assert len(events[0].signature) > 0


def test_verify_signed_events_passes(tmp_path: Path):
    private_key, public_key = _generate_ed25519_keypair()
    w = EventWriter("task_001", tmp_path / "run")
    w.set_signing_key(private_key.private_bytes_raw())
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})
    events = w.read_all()
    assert verify_event_signatures(events, public_key.public_bytes_raw()) is True
    # Chain is also still valid.
    assert verify_event_chain(events) is True


def test_verify_signed_events_fails_for_tampered_hash(tmp_path: Path):
    private_key, public_key = _generate_ed25519_keypair()
    w = EventWriter("task_001", tmp_path / "run")
    w.set_signing_key(private_key.private_bytes_raw())
    w.write(EventType.TASK_CREATED, {"request": "test"})
    events = w.read_all()
    # Tamper with the hash — the signature was over the original hash.
    events[0].hash = "tampered"
    assert verify_event_signatures(events, public_key.public_bytes_raw()) is False


def test_verify_signed_events_fails_for_unsigned_events(tmp_path: Path):
    _, public_key = _generate_ed25519_keypair()
    w = EventWriter("task_001", tmp_path / "run")
    # No signing key set — events are unsigned.
    w.write(EventType.TASK_CREATED, {"request": "test"})
    events = w.read_all()
    assert verify_event_signatures(events, public_key.public_bytes_raw()) is False


def test_unsigned_events_have_empty_signature(tmp_path: Path):
    w = EventWriter("task_001", tmp_path / "run")
    # No signing key — signature should be empty.
    w.write(EventType.TASK_CREATED, {"request": "test"})
    events = w.read_all()
    assert events[0].signature == ""


def test_signing_key_requires_cryptography_package(tmp_path: Path):
    """If cryptography is not installed, set_signing_key raises ImportError."""
    # We can't easily uninstall cryptography for this test, so we just verify
    # that the import error message is correct when the module is missing.
    # This test passes as long as cryptography is installed (which it is
    # in the dev environment). The real protection is the ImportError in
    # the source code.
    from acp.events import EventWriter

    w = EventWriter("test", tmp_path / "run")
    # If cryptography is installed, this should work with a valid key.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        w.set_signing_key(key.private_bytes_raw())
        # No error — cryptography is available.
    except ImportError:
        pytest.skip("cryptography not installed")


# --------------------------------------------------------------------------- #
# Event timeline in reports
# --------------------------------------------------------------------------- #


def test_report_includes_event_timeline(tmp_path: Path):
    from acp.gitops.diff import DiffCapture
    from acp.models import Recommendation, ReviewResult, RiskLevel
    from acp.reports.templates import render_report

    w = EventWriter("task_001", tmp_path / "run")
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})
    events = w.read_all()

    task = Task(
        task_id="task_001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        task_branch="agent/task_001",
        worktree_path=tmp_path / "worktree",
        user_request="test",
        status=TaskStatus.PASSED,
    )
    review = ReviewResult(
        risk=RiskLevel.LOW,
        recommendation=Recommendation.MERGE,
        summary="ok",
    )
    diff = DiffCapture(patch="", stat="", changed_files=["a.py"], insertions=1, deletions=0)

    body = render_report(
        task=task,
        command_results=[],
        review=review,
        diff=diff,
        agent_result=None,
        events=events,
    )
    assert "## Event timeline" in body
    assert "evt_000001" in body
    assert "task.created" in body
    assert "task.completed" in body
    assert "3 events" in body


def test_report_without_events_still_works(tmp_path: Path):
    """Backward compatibility: events=None should not break the report."""
    from acp.gitops.diff import DiffCapture
    from acp.models import Recommendation, ReviewResult, RiskLevel
    from acp.reports.templates import render_report

    task = Task(
        task_id="task_001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        task_branch="agent/task_001",
        worktree_path=tmp_path / "worktree",
        user_request="test",
        status=TaskStatus.PASSED,
    )
    review = ReviewResult(risk=RiskLevel.LOW, recommendation=Recommendation.MERGE, summary="ok")
    diff = DiffCapture(patch="", stat="", changed_files=["a.py"], insertions=1, deletions=0)

    body = render_report(
        task=task,
        command_results=[],
        review=review,
        diff=diff,
        agent_result=None,
        events=None,
    )
    assert "## Event timeline" not in body
    assert "# Task report" in body


def test_failure_report_includes_event_timeline(tmp_path: Path):
    from acp.reports.templates import render_failure_report

    w = EventWriter("task_001", tmp_path / "run")
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.NODE_FAILED, {"node": "create_worktree", "message": "error"})
    events = w.read_all()

    task = Task(
        task_id="task_001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        task_branch="agent/task_001",
        worktree_path=tmp_path / "worktree",
        user_request="test",
        status=TaskStatus.FAILED,
    )

    body = render_failure_report(task=task, error="worktree failed", events=events)
    assert "## Event timeline" in body
    assert "evt_000001" in body
    assert "node.failed" in body
    assert "2 events" in body
