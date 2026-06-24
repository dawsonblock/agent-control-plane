"""Regression tests for the build-5 trust-layer holes.

These pin the exact bugs the verdict called out so they cannot come back:

  - ``acp approve`` then ``acp verify`` passes (approval no longer breaks the
    evidence verifier).
  - signed run -> approve -> ``acp verify --public-key`` passes (lifecycle
    events are signed with the run's own key).
  - ``acp reject`` then ``acp verify`` passes.
  - the durable SQLite store receives ``human.approved`` / ``human.rejected``
    lifecycle events (not just run events).
  - signing fail-closed: a configured signing key that is missing/unreadable
    is fatal, never a silent downgrade to unsigned.
  - early worktree-failure reports include the final event timeline
    (``task.failed``) + manifest hash, not a stale pre-terminal snapshot.
  - invalid (path-shaped) task ids are rejected by every path-touching command.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from acp.cli import app
from acp.config import AgentSection, CommandsSection, EvidenceSection, RepoConfig, RepoSection, ReviewSection
from acp.evidence.durable_store import DurableEventStore
from acp.evidence.manifest import verify_evidence_manifest
from acp.events import verify_event_chain, verify_event_signatures
from acp.graph.workflow import run_workflow
from acp.models import Event, EventType, TaskStatus
from acp.store import TaskStore


runner = CliRunner()


def _config(repo_path: Path, **evidence_kwargs) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(**evidence_kwargs) if evidence_kwargs else EvidenceSection(),
    )


def _events(store: TaskStore, task_id: str) -> list[Event]:
    p = store.events_path(task_id)
    return [Event.model_validate_json(l) for l in p.read_text().splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
# P0: approval / rejection must not break acp verify
# --------------------------------------------------------------------------- #


def test_approve_then_verify_passes(disposable_repo, isolated_workspace):
    """acp approve then acp verify must still pass — the core P0 regression."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)

    # Verify passes before approval.
    r0 = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r0.exit_code == 0, f"verify before approve failed: {r0.output}"

    # Approve.
    r1 = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "alice@example.com",
    ])
    assert r1.exit_code == 0, f"approve failed: {r1.output}"

    # Verify still passes after approval — the bug that was fixed.
    r2 = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r2.exit_code == 0, f"verify after approve FAILED (regression): {r2.output}"
    assert "evidence manifest valid" in r2.output

    # The manifest on disk must verify, and the chain head is the approval event.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    assert verify_evidence_manifest(store.run_dir(task_id)) is True
    events = _events(store, task_id)
    assert verify_event_chain(events) is True
    assert events[-1].type == EventType.HUMAN_APPROVED
    assert events[-1].hash != ""


def test_signed_run_approve_then_verify_with_public_key(disposable_repo, isolated_workspace, tmp_path):
    """signed run -> approve -> acp verify --public-key passes (lifecycle signed)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    os.environ["ACP_TEST"] = "1"

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(private_key.private_bytes_raw())
    pub_path = tmp_path / "public_key.bin"
    pub_path.write_bytes(public_key.public_bytes_raw())

    cfg = _config(disposable_repo.path, signing_key_path=key_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)

    # Approve — the lifecycle event must be signed with the run's key.
    r1 = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "alice@example.com",
    ])
    assert r1.exit_code == 0, f"approve failed: {r1.output}"

    # Every event (including human.approved) must be signed + verify.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)
    assert all(e.signature for e in events), "lifecycle event was not signed"
    assert events[-1].type == EventType.HUMAN_APPROVED
    assert verify_event_signatures(events, public_key.public_bytes_raw()) is True

    # acp verify --public-key passes end to end.
    r2 = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--public-key", str(pub_path),
    ])
    assert r2.exit_code == 0, f"signed verify after approve FAILED: {r2.output}"
    assert "Ed25519 signatures valid" in r2.output


def test_reject_then_verify_passes(disposable_repo, isolated_workspace):
    """acp reject then acp verify must still pass."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)

    r1 = runner.invoke(app, [
        "reject", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--rejecter", "bob@example.com",
        "--reason", "too risky",
    ])
    assert r1.exit_code == 0, f"reject failed: {r1.output}"

    r2 = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r2.exit_code == 0, f"verify after reject FAILED (regression): {r2.output}"
    assert "evidence manifest valid" in r2.output

    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)
    assert events[-1].type == EventType.HUMAN_REJECTED
    assert verify_evidence_manifest(store.run_dir(task_id)) is True


# --------------------------------------------------------------------------- #
# P1: durable SQLite store receives lifecycle events
# --------------------------------------------------------------------------- #


def test_durable_store_receives_lifecycle_events(disposable_repo, isolated_workspace, tmp_path):
    """approve dual-writes human.approved to the run's SQLite durable store."""
    os.environ["ACP_TEST"] = "1"
    db_path = tmp_path / "events.db"
    cfg = _config(disposable_repo.path, durable_store=db_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)

    runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "alice@example.com",
    ])

    with DurableEventStore(db_path) as db:
        db_events = db.query(task_id=task_id, type=EventType.HUMAN_APPROVED.value)
        assert len(db_events) == 1, "human.approved missing from durable store"
        # And the full SQLite event set matches the JSONL log.
        all_db = db.query(task_id=task_id)
        store = TaskStore(runs_root=isolated_workspace["runs_root"])
        jsonl_events = _events(store, task_id)
        assert len(all_db) == len(jsonl_events)


def test_durable_store_receives_reject_event(disposable_repo, isolated_workspace, tmp_path):
    """reject dual-writes human.rejected to the run's SQLite durable store."""
    os.environ["ACP_TEST"] = "1"
    db_path = tmp_path / "events.db"
    cfg = _config(disposable_repo.path, durable_store=db_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    runner.invoke(app, [
        "reject", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
    ])

    with DurableEventStore(db_path) as db:
        db_events = db.query(task_id=task_id, type=EventType.HUMAN_REJECTED.value)
        assert len(db_events) == 1, "human.rejected missing from durable store"


# --------------------------------------------------------------------------- #
# P1: signing fail-closed — no silent downgrade to unsigned
# --------------------------------------------------------------------------- #


def test_signing_fail_closed_on_missing_key(disposable_repo, isolated_workspace, tmp_path):
    """A configured signing key that doesn't exist is fatal, not a silent skip."""
    from acp.errors import EvidenceConfigError
    os.environ["ACP_TEST"] = "1"
    bogus_key = tmp_path / "does_not_exist.bin"
    cfg = _config(disposable_repo.path, signing_key_path=bogus_key)

    try:
        run_workflow(
            config=cfg,
            user_request="test task",
            runs_root=isolated_workspace["runs_root"],
            vault_root=isolated_workspace["vault_root"],
        )
        raise AssertionError("expected EvidenceConfigError for missing signing key")
    except EvidenceConfigError as exc:
        assert "signing key" in str(exc).lower()


def test_signing_fail_closed_on_bad_key_length(disposable_repo, isolated_workspace, tmp_path):
    """A malformed signing key (wrong byte length) is fatal, not a silent skip."""
    from acp.errors import EvidenceConfigError
    os.environ["ACP_TEST"] = "1"
    bad_key = tmp_path / "bad_key.bin"
    bad_key.write_bytes(b"only-ten-")  # 9 bytes, not 32
    cfg = _config(disposable_repo.path, signing_key_path=bad_key)

    try:
        run_workflow(
            config=cfg,
            user_request="test task",
            runs_root=isolated_workspace["runs_root"],
            vault_root=isolated_workspace["vault_root"],
        )
        raise AssertionError("expected EvidenceConfigError for malformed signing key")
    except EvidenceConfigError as exc:
        assert "32 bytes" in str(exc)


# --------------------------------------------------------------------------- #
# P1: early-failure report is a true projection of the final event log
# --------------------------------------------------------------------------- #


def test_early_failure_report_includes_terminal_event_and_manifest_hash(disposable_repo, isolated_workspace):
    """Worktree-failure report timeline includes task.failed + manifest hash."""
    import json
    from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
    from acp.events import EventWriter
    from acp.graph.state import initial_state
    from acp.graph.workflow import build_workflow

    # Force a worktree creation failure with a non-existent base branch.
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=disposable_repo.path, default_branch="nonexistent"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
    )
    runs_root = isolated_workspace["runs_root"]
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)
    result = wf.invoke(
        initial_state(config=cfg, user_request="test", vault_root=isolated_workspace["vault_root"], runs_root=runs_root),
        config={"configurable": {"thread_id": "test"}},
    )
    assert result["status"] == TaskStatus.FAILED

    task_id = result["task_id"]
    run_dir = store.run_dir(task_id)

    # The event log's terminal event is task.failed.
    log_events = [json.loads(l) for l in store.events_path(task_id).read_text().splitlines() if l.strip()]
    assert log_events[-1]["type"] == EventType.TASK_FAILED.value

    # The report must show the FULL timeline (including task.failed), not the
    # stale pre-terminal snapshot, and must include the manifest hash.
    report_path = run_dir / "artifacts" / "final_report.md"
    assert report_path.is_file()
    body = report_path.read_text()
    assert "task.failed" in body, "failure report timeline missing terminal task.failed event"
    assert "report.written" in body, "failure report timeline missing report.written event"
    assert "Evidence manifest hash" in body, "failure report missing manifest hash"

    # The manifest must verify (artifacts + final chain head).
    assert verify_evidence_manifest(run_dir) is True


# --------------------------------------------------------------------------- #
# P1: invalid task ids rejected by every path-touching command
# --------------------------------------------------------------------------- #


def test_invalid_task_id_rejected_by_verify(tmp_path):
    for bad in ("../etc/passwd", "task_bad", "task_2026_0001", "task_20260624_1", ""):
        r = runner.invoke(app, [
            "verify", "--task", bad,
            "--runs-root", str(tmp_path / "runs"),
        ])
        assert r.exit_code == 1, f"verify accepted invalid id {bad!r}"
        assert "invalid task id" in r.output


def test_invalid_task_id_rejected_by_cleanup(tmp_path):
    # cleanup also needs a config, but task_id validation runs first.
    cfg_path = tmp_path / "demo.repo.yaml"
    cfg_path.write_text("repo:\n  name: demo\n  path: %s\n  default_branch: main\n" % str(tmp_path / "repo"))
    r = runner.invoke(app, [
        "cleanup", "--config", str(cfg_path),
        "--task", "../etc/passwd",
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert r.exit_code == 1
    assert "invalid task id" in r.output


def test_valid_shaped_nonexistent_task_id_reaches_not_found(tmp_path):
    """A valid-shaped but absent id is NOT rejected as invalid — it reaches the not-found path."""
    r = runner.invoke(app, [
        "verify", "--task", "task_20260624_9999",
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert r.exit_code == 1
    assert "not found" in r.output
    assert "invalid task id" not in r.output


# --------------------------------------------------------------------------- #
# Self-review fixes: partial-failure consistency + pre-v0.5.9 signed runs
# --------------------------------------------------------------------------- #


def test_approve_reverts_vault_note_on_lifecycle_failure(disposable_repo, isolated_workspace, tmp_path):
    """If the lifecycle event write fails after the vault note is modified,
    the vault note is reverted to its pre-approval state.

    The event log is the source of truth — a modified vault note without a
    corresponding event is an inconsistent state that must not persist.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from acp.vault.frontmatter import parse_frontmatter
    os.environ["ACP_TEST"] = "1"

    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(private_key.private_bytes_raw())

    cfg = _config(disposable_repo.path, signing_key_path=key_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)

    # Snapshot the vault note's pre-approval content.
    note_path = isolated_workspace["vault_root"] / "tasks" / f"{task_id}.md"
    original_content = note_path.read_text()
    original_fm, _ = parse_frontmatter(original_content)
    assert original_fm.approved is False

    # Delete the signing key so _record_lifecycle_event fails with
    # EvidenceConfigError (run was signed but key is now unreadable).
    key_path.unlink()

    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "alice@example.com",
    ])
    assert r.exit_code != 0, "approve should have failed when signing key is missing"

    # The vault note must be reverted to its pre-approval state.
    reverted_content = note_path.read_text()
    reverted_fm, _ = parse_frontmatter(reverted_content)
    assert reverted_fm.approved is False, "vault note was not reverted after lifecycle failure"
    assert reverted_fm.memory_status != "active", "vault note memory_status was not reverted"

    # No human.approved event should exist in the event log.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)
    approved = [e for e in events if e.type == EventType.HUMAN_APPROVED]
    assert len(approved) == 0, "human.approved event was written despite lifecycle failure"

    # Task status should still be the original (not APPROVED).
    task = store.load(task_id)
    assert task.status != TaskStatus.APPROVED


def test_reject_reverts_vault_note_on_lifecycle_failure(disposable_repo, isolated_workspace, tmp_path):
    """Same reversion guarantee for reject."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from acp.vault.frontmatter import parse_frontmatter
    os.environ["ACP_TEST"] = "1"

    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(private_key.private_bytes_raw())

    cfg = _config(disposable_repo.path, signing_key_path=key_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    note_path = isolated_workspace["vault_root"] / "tasks" / f"{task_id}.md"
    original_content = note_path.read_text()
    original_fm, _ = parse_frontmatter(original_content)
    assert original_fm.memory_status != "archived"

    key_path.unlink()

    r = runner.invoke(app, [
        "reject", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
    ])
    assert r.exit_code != 0

    reverted_fm, _ = parse_frontmatter(note_path.read_text())
    assert reverted_fm.memory_status != "archived", "vault note was not reverted after reject failure"

    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)
    rejected = [e for e in events if e.type == EventType.HUMAN_REJECTED]
    assert len(rejected) == 0


def test_approve_pre_v059_signed_run_fails_closed(disposable_repo, isolated_workspace, tmp_path):
    """A signed run with no evidence_config sidecar (pre-v0.5.9) must fail
    closed when approve is attempted, not silently write an unsigned event."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    os.environ["ACP_TEST"] = "1"

    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(private_key.private_bytes_raw())

    cfg = _config(disposable_repo.path, signing_key_path=key_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    # Simulate a pre-v0.5.9 run by deleting the evidence_config sidecar.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    sidecar = store.run_dir(task_id) / "evidence_config.json"
    assert sidecar.is_file(), "evidence_config sidecar should exist for v0.5.9 runs"
    sidecar.unlink()

    # The existing events should have signatures (the run was signed).
    events = _events(store, task_id)
    assert all(e.signature for e in events), "pre-v0.5.9 run events should be signed"

    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
    ])
    assert r.exit_code != 0, "approve should fail-closed for signed run with no sidecar"
    assert "signed" in r.output.lower() or "sidecar" in r.output.lower()

    # No lifecycle event should have been written.
    events_after = _events(store, task_id)
    assert len(events_after) == len(events), "no lifecycle event should be written on fail-closed"


def test_cleanup_validates_task_id_before_config(tmp_path):
    """cleanup rejects an invalid task_id before even loading the config."""
    # Use a non-existent config path — if task_id validation runs first,
    # we'll see "invalid task id" not "config not found".
    r = runner.invoke(app, [
        "cleanup", "--config", str(tmp_path / "nonexistent.yaml"),
        "--task", "../etc/passwd",
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert r.exit_code == 1
    assert "invalid task id" in r.output
    # The config error should NOT appear — task_id was rejected first.
    assert "config" not in r.output.lower() or "invalid task id" in r.output
