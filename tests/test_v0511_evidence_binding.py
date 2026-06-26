"""v0.5.11 acceptance criteria — full evidence binding.

Tests the 10 acceptance criteria from the v0.5.11 spec:

  1. Missing evidence_manifest.json fails verification for v0.5.10+ runs.
  2. Missing final_report.md fails verification.
  3. Edited final_report.md fails verification.
  4. Edited task.json content fails verification unless the edit is an
     explicitly signed lifecycle state transition.
  5. Lifecycle events require lifecycle_manifest.json.
  6. Deleted lifecycle_manifest.json fails verification when lifecycle
     events exist.
  7. durable_mode is persisted in evidence_config.json.
  8. Approval/rejection fail closed if durable_mode=required and SQLite
     write fails.
  9. Malformed events.jsonl prevents signature success output.
  10. pyproject version matches acp.__version__.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from acp.cli import app
from acp.config import AgentSection, CommandsSection, DurableMode, EvidenceSection, RepoConfig, RepoSection, ReviewSection
from acp.evidence.manifest import (
    read_evidence_config,
    verify_evidence_manifest,
)
from acp.graph.workflow import run_workflow
from acp.models import Event, EventType
from acp.store import TaskStore


runner = CliRunner()


def _config(repo_path: Path, **evidence_kwargs) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(require_human_approval=False),
        evidence=EvidenceSection(**evidence_kwargs) if evidence_kwargs else EvidenceSection(),
    )


def _events(store: TaskStore, task_id: str) -> list[Event]:
    p = store.events_path(task_id)
    return [Event.model_validate_json(l) for l in p.read_text().splitlines() if l.strip()]


def _run(disposable_repo, isolated_workspace, **evidence_kwargs) -> tuple[str, TaskStore, Path]:
    """Run a workflow and return (task_id, store, run_dir)."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path, **evidence_kwargs)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)
    return task_id, store, run_dir


# --------------------------------------------------------------------------- #
# 1. Missing evidence_manifest.json fails for v0.5.10+ runs
# --------------------------------------------------------------------------- #


def test_missing_manifest_fails_verify(disposable_repo, isolated_workspace):
    """Deleting evidence_manifest.json must fail acp verify for runs with
    evidence.finalized (v0.5.10+)."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Verify passes before deletion.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0

    # Delete the manifest.
    (run_dir / "evidence_manifest.json").unlink()

    # Verify must fail — not just warn.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1
    assert "evidence manifest not found" in r.output
    assert "required" in r.output


# --------------------------------------------------------------------------- #
# 2. Missing final_report.md fails verification
# --------------------------------------------------------------------------- #


def test_missing_report_fails_verify(disposable_repo, isolated_workspace):
    """Deleting final_report.md must fail acp verify."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Verify passes before deletion.
    assert verify_evidence_manifest(run_dir) is True

    # Delete the report.
    (run_dir / "artifacts" / "final_report.md").unlink()

    # verify_evidence_manifest must fail (report_bound event's report_hash
    # can't match a missing file).
    assert verify_evidence_manifest(run_dir) is False

    # CLI verify must also fail.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1


# --------------------------------------------------------------------------- #
# 3. Edited final_report.md fails verification
# --------------------------------------------------------------------------- #


def test_edited_report_fails_verify(disposable_repo, isolated_workspace):
    """Editing final_report.md must fail acp verify."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Verify passes before editing.
    assert verify_evidence_manifest(run_dir) is True

    # Edit the report.
    report_path = run_dir / "artifacts" / "final_report.md"
    original = report_path.read_text()
    report_path.write_text(original + "\n# TAMPERED\n")

    # verify_evidence_manifest must fail.
    assert verify_evidence_manifest(run_dir) is False

    # CLI verify must also fail.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1


# --------------------------------------------------------------------------- #
# 4. Edited task.json content fails verification (unless lifecycle status)
# --------------------------------------------------------------------------- #


def test_edited_task_json_user_request_fails_verify(disposable_repo, isolated_workspace):
    """Editing task.json user_request must fail verify — it's an immutable
    identity field."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    assert verify_evidence_manifest(run_dir) is True

    # Tamper with user_request (immutable field).
    task_json_path = run_dir / "task.json"
    data = json.loads(task_json_path.read_text())
    data["user_request"] = "TAMPERED REQUEST"
    task_json_path.write_text(json.dumps(data, indent=2) + "\n")

    assert verify_evidence_manifest(run_dir) is False


def test_task_json_status_change_allowed_with_lifecycle(disposable_repo, isolated_workspace):
    """Changing task.json status during a signed lifecycle event (approve)
    must NOT fail verify — status is a lifecycle-mutable field."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Approve the task (this changes status to approved).
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 0, f"approve failed: {r.output}"

    # Verify must still pass — status change is a lifecycle transition.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0, f"verify after approve failed: {r.output}"


def test_task_json_repo_name_tamper_fails_verify(disposable_repo, isolated_workspace):
    """Editing task.json repo_name must fail verify — it's an immutable
    identity field."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    assert verify_evidence_manifest(run_dir) is True

    # Tamper with repo_name (immutable field).
    task_json_path = run_dir / "task.json"
    data = json.loads(task_json_path.read_text())
    data["repo_name"] = "TAMPERED"
    task_json_path.write_text(json.dumps(data, indent=2) + "\n")

    assert verify_evidence_manifest(run_dir) is False


# --------------------------------------------------------------------------- #
# 5. Lifecycle events require lifecycle_manifest.json
# --------------------------------------------------------------------------- #


def test_lifecycle_events_require_lifecycle_manifest(disposable_repo, isolated_workspace):
    """After approval, lifecycle_manifest.json must exist. Deleting it must
    fail verify."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Approve.
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 0

    # Lifecycle manifest exists.
    assert (run_dir / "lifecycle_manifest.json").is_file()

    # Verify passes.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0

    # Delete the lifecycle manifest.
    (run_dir / "lifecycle_manifest.json").unlink()

    # Verify must fail — lifecycle events exist but no lifecycle manifest.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1
    assert "lifecycle manifest not found" in r.output
    assert "required" in r.output


# --------------------------------------------------------------------------- #
# 6. (Covered by test 5 — same test deletes lifecycle manifest)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 7. durable_mode is persisted in evidence_config.json
# --------------------------------------------------------------------------- #


def test_durable_mode_persisted_in_evidence_config(disposable_repo, isolated_workspace, tmp_path):
    """durable_mode must be persisted in evidence_config.json at finalize."""
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.REQUIRED,
    )

    ev_cfg = read_evidence_config(run_dir)
    assert ev_cfg["durable_mode"] == "required"
    assert ev_cfg["durable_store"] == db_path.resolve()


def test_durable_mode_best_effort_persisted(disposable_repo, isolated_workspace, tmp_path):
    """durable_mode=best_effort is persisted."""
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.BEST_EFFORT,
    )

    ev_cfg = read_evidence_config(run_dir)
    assert ev_cfg["durable_mode"] == "best_effort"


def test_durable_mode_none_when_not_configured(disposable_repo, isolated_workspace):
    """When no durable store is configured, durable_store is None and
    durable_mode is the default (best_effort)."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    ev_cfg = read_evidence_config(run_dir)
    assert ev_cfg["durable_store"] is None
    # durable_mode defaults to best_effort even without a store — it only
    # matters when durable_store is set.
    assert ev_cfg["durable_mode"] in (None, "best_effort")


# --------------------------------------------------------------------------- #
# 8. Approval fails closed if durable_mode=required and SQLite write fails
# --------------------------------------------------------------------------- #


def test_approve_fails_closed_durable_required(disposable_repo, isolated_workspace, tmp_path):
    """When durable_mode=required, approval must fail if the SQLite store
    is unavailable — not just warn. And the human.approved event must be
    rolled back from events.jsonl so the run is not left half-approved.

    Note: we break the actual DB file (not the config) because
    evidence_config.json is now bound to the signed event log —
    changing it would be correctly detected as tampering.
    """
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.REQUIRED,
    )

    # Count events before the failed approval.
    events_before = _events(store, task_id)
    count_before = len(events_before)

    # Corrupt the DB file so SQLite can't open it. We don't modify
    # evidence_config.json — that would break the config hash binding.
    # Save the original DB so we can restore it after the failed approval
    # (the corruption is test setup, not a result of the approval).
    db_backup = db_path.read_bytes()
    db_path.unlink()
    db_path.write_text("not a sqlite database")

    # Approve must fail (not just warn).
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 1, f"approve should have failed closed: {r.output}"
    assert "all evidence restored" in r.output

    # CRITICAL: the human.approved event must NOT be in the event log.
    events_after = _events(store, task_id)
    assert len(events_after) == count_before, (
        f"event log was not rolled back: {count_before} events before, "
        f"{len(events_after)} after. The half-approved bug is present."
    )
    assert not any(e.type == EventType.HUMAN_APPROVED for e in events_after), (
        "human.approved event survived the rollback — half-approved state!"
    )

    # The event chain must still be valid (rollback restored prev_hash).
    from acp.events import verify_event_chain
    assert verify_event_chain(events_after) is True, (
        "event chain broken after rollback"
    )

    # Restore the DB (the corruption was test setup, not a result of the
    # approval — we need a valid DB for verify to check durable consistency).
    db_path.write_bytes(db_backup)

    # Verify must still pass (the run is in its pre-approval state).
    r2 = runner.invoke(app, ["verify", "--task", task_id,
                             "--runs-root", str(isolated_workspace["runs_root"])])
    assert r2.exit_code == 0, f"verify should pass after rollback: {r2.output}"


# --------------------------------------------------------------------------- #
# 8b. Second durable-write failure — full evidence rollback
# --------------------------------------------------------------------------- #


def test_second_durable_write_failure_full_rollback(disposable_repo, isolated_workspace, tmp_path):
    """The critical regression test from the v0.5.11 review:

    If the SQLite write for the SECOND lifecycle event (evidence.report_bound)
    fails — after the first event (human.approved) was already written to
    both events.jsonl and SQLite, and the report was already re-rendered —
    ALL evidence must be restored to the pre-approval state:

      * events.jsonl: no human.approved, no evidence.report_bound
      * final_report.md: original content (no human.approved in timeline)
      * SQLite: no orphan human.approved
      * lifecycle_manifest.json: original or absent
      * acp verify: passes (run is in pre-approval state)
    """
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.REQUIRED,
    )

    # Count events and save report content before approval.
    events_before = _events(store, task_id)
    count_before = len(events_before)
    report_path = run_dir / "artifacts" / "final_report.md"
    report_before = report_path.read_bytes()

    # Pre-seed the SQLite store with a duplicate of the event_id that the
    # evidence.report_bound event will use. The next event_id after the
    # current count will be evt_{count+1:06d}, and the one after that will
    # be evt_{count+2:06d}. We pre-insert a row with the SECOND event_id
    # (the report_bound one) so that the first durable write (human.approved)
    # succeeds but the second (evidence.report_bound) fails with a duplicate
    # key constraint.
    from acp.evidence.durable_store import DurableEventStore
    from acp.models import Event, EventType as ET

    # Determine what event_ids the approval will use.
    # human.approved will be evt_{count+1:06d}, report_bound will be evt_{count+2:06d}
    report_bound_id = f"evt_{count_before + 2:06d}"

    # Pre-seed the duplicate.
    with DurableEventStore(db_path) as db:
        # Insert a dummy event with the same (task_id, event_id) that the
        # report_bound event will use. This will cause the second append to fail.
        dummy = Event(
            event_id=report_bound_id,
            task_id=task_id,
            type=ET.TASK_CREATED,  # type doesn't matter, just the PK collision
            timestamp="2025-01-01T00:00:00Z",
            payload={},
            prev_hash="GENESIS",
            hash="dummy_hash",
            signature="",
        )
        db.append(dummy)

    # Now run approve. The first durable write (human.approved) should succeed,
    # the report gets re-rendered, then the second durable write
    # (evidence.report_bound) fails due to the duplicate key.
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 1, f"approve should have failed: {r.output}"
    assert "all evidence restored" in r.output

    # 1. events.jsonl: no human.approved, no extra evidence.report_bound.
    events_after = _events(store, task_id)
    assert len(events_after) == count_before, (
        f"event log not rolled back: {count_before} before, "
        f"{len(events_after)} after"
    )
    assert not any(e.type == EventType.HUMAN_APPROVED for e in events_after), (
        "human.approved survived rollback!"
    )
    # The original run may have one evidence.report_bound; the lifecycle
    # write adds a second. After rollback, the count should match the original.
    report_bound_after = [e for e in events_after if e.type == EventType.EVIDENCE_REPORT_BOUND]
    report_bound_before = [e for e in events_before if e.type == EventType.EVIDENCE_REPORT_BOUND]
    assert len(report_bound_after) == len(report_bound_before), (
        f"evidence.report_bound count changed: {len(report_bound_before)} before, "
        f"{len(report_bound_after)} after — lifecycle report_bound not rolled back!"
    )

    # 2. final_report.md: must be the original content.
    report_after = report_path.read_bytes()
    assert report_after == report_before, (
        "final_report.md was not restored to pre-approval state after rollback!"
    )

    # 3. SQLite: no orphan human.approved, no extra report_bound.
    # The original run may have a report_bound in SQLite; the lifecycle write
    # would add a second. After rollback, only the original should remain.
    with DurableEventStore(db_path) as db:
        db_approved = db.query(task_id=task_id, type=EventType.HUMAN_APPROVED.value)
        assert len(db_approved) == 0, (
            f"orphan human.approved in SQLite: {len(db_approved)} events"
        )
        db_report_bound = db.query(task_id=task_id, type=EventType.EVIDENCE_REPORT_BOUND.value)
        # The original run has 1 report_bound. The pre-seeded dummy has a
        # different type (task.created). So after rollback, we should have
        # exactly 1 (the original) — not 2 (original + lifecycle).
        assert len(db_report_bound) == 1, (
            f"expected 1 report_bound in SQLite (original), got {len(db_report_bound)} "
            f"— lifecycle report_bound not rolled back from SQLite!"
        )

    # 4. Event chain still valid.
    from acp.events import verify_event_chain
    assert verify_event_chain(events_after) is True, "event chain broken after rollback"

    # 5. Clean up the pre-seeded dummy event from SQLite (it was only needed
    # to trigger the duplicate key failure). After cleanup, SQLite should
    # match events.jsonl exactly.
    with DurableEventStore(db_path) as db:
        db._conn.execute(
            "DELETE FROM events WHERE task_id = ? AND event_id = ?",
            (task_id, report_bound_id),
        )

    # 6. acp verify passes (run is in pre-approval state, SQLite is clean).
    r2 = runner.invoke(app, ["verify", "--task", task_id,
                             "--runs-root", str(isolated_workspace["runs_root"])])
    assert r2.exit_code == 0, f"verify should pass after rollback: {r2.output}"


# --------------------------------------------------------------------------- #
# 9. Malformed events.jsonl prevents signature success output
# --------------------------------------------------------------------------- #


def test_malformed_events_suppresses_signature_success(disposable_repo, isolated_workspace, tmp_path):
    """When the event log is malformed, verify must NOT print 'signatures
    valid' — it should print 'signature verification skipped'."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    os.environ["ACP_TEST"] = "1"
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(private_key.private_bytes_raw())
    pub_path = tmp_path / "public_key.bin"
    pub_path.write_bytes(public_key.public_bytes_raw())

    task_id, store, run_dir = _run(disposable_repo, isolated_workspace, signing_key_path=key_path)

    # Append a malformed line to the event log.
    events_path = store.events_path(task_id)
    with events_path.open("a") as f:
        f.write("{NOT VALID JSON}\n")

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--public-key", str(pub_path),
    ])
    assert r.exit_code == 1
    # Must NOT print "signatures valid".
    assert "signatures valid" not in r.output
    # Must print the skip message.
    assert "signature verification skipped" in r.output
    assert "malformed" in r.output


# --------------------------------------------------------------------------- #
# 10. pyproject version matches acp.__version__
# --------------------------------------------------------------------------- #


def test_pyproject_version_matches_acp_version():
    """pyproject.toml version must match acp.__version__."""
    import tomllib
    from acp import __version__

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["project"]["version"] == __version__, (
        f"pyproject.toml version {data['project']['version']!r} != "
        f"acp.__version__ {__version__!r}"
    )


# --------------------------------------------------------------------------- #
# Additional: evidence.finalized payload includes task_json_hash
# --------------------------------------------------------------------------- #


def test_evidence_finalized_includes_task_json_hash(disposable_repo, isolated_workspace):
    """The evidence.finalized event must include task_json_hash."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)
    events = _events(store, task_id)

    finalized = [e for e in events if e.type == EventType.EVIDENCE_FINALIZED]
    assert len(finalized) == 1
    assert "task_json_hash" in finalized[0].payload
    assert finalized[0].payload["task_json_hash"] is not None


# --------------------------------------------------------------------------- #
# Additional: evidence.report_bound event is written
# --------------------------------------------------------------------------- #


def test_evidence_report_bound_event_written(disposable_repo, isolated_workspace):
    """A completed run must write an evidence.report_bound event with
    report_hash."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)
    events = _events(store, task_id)

    report_bound = [e for e in events if e.type == EventType.EVIDENCE_REPORT_BOUND]
    assert len(report_bound) == 1, "evidence.report_bound event must be written"
    assert "report_hash" in report_bound[0].payload
    assert report_bound[0].payload["report_hash"] is not None


# --------------------------------------------------------------------------- #
# Additional: --deep mode recomputes individual artifact hashes
# --------------------------------------------------------------------------- #


def test_deep_mode_detects_extra_artifact_file(disposable_repo, isolated_workspace):
    """Adding an extra artifact file must fail verify in both fast and deep
    modes — the artifact_content_hash covers all files under artifacts/."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Add an extra file to artifacts/ (not in the manifest).
    (run_dir / "artifacts" / "extra_junk.txt").write_text("extra")

    # Both modes fail — artifact_content_hash includes all artifact files,
    # so an extra file changes the hash and breaks the signed event binding.
    assert verify_evidence_manifest(run_dir, deep=False) is False
    assert verify_evidence_manifest(run_dir, deep=True) is False


def test_deep_mode_detects_tampered_artifact_hash(disposable_repo, isolated_workspace):
    """--deep mode must detect a tampered artifact by individual hash."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Tamper with an artifact (but keep the same file — just change content).
    artifacts_dir = run_dir / "artifacts"
    artifact_files = [p for p in artifacts_dir.rglob("*") if p.is_file() and p.name != "final_report.md"]
    assert artifact_files
    target = artifact_files[0]
    original = target.read_bytes()
    target.write_bytes(original + b"\n# tampered\n")

    # Fast mode: fails because artifact_content_hash doesn't match.
    assert verify_evidence_manifest(run_dir, deep=False) is False
    # Deep mode: also fails.
    assert verify_evidence_manifest(run_dir, deep=True) is False


# --------------------------------------------------------------------------- #
# Additional: report_hash binding survives after lifecycle (via lifecycle manifest)
# --------------------------------------------------------------------------- #


def test_report_tamper_after_approval_fails_verify(disposable_repo, isolated_workspace):
    """After approval, editing the report must still fail verify — the
    lifecycle manifest's report_hash catches it."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Approve.
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 0

    # Verify passes after approval.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0

    # Tamper with the report.
    report_path = run_dir / "artifacts" / "final_report.md"
    original = report_path.read_text()
    report_path.write_text(original + "\n# TAMPERED AFTER APPROVAL\n")

    # Verify must fail — lifecycle manifest's report_hash catches it.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1


# --------------------------------------------------------------------------- #
# Additional: tampering with lifecycle manifest report_hash fails verify
# --------------------------------------------------------------------------- #


def test_lifecycle_manifest_report_hash_tamper_fails_verify(disposable_repo, isolated_workspace):
    """Tampering with the report after lifecycle must fail verify even if the
    attacker also edits the lifecycle manifest — the second signed
    evidence.report_bound event's report_hash can't be forged."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # Approve.
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 0

    # Tamper with the report.
    report_path = run_dir / "artifacts" / "final_report.md"
    original = report_path.read_text()
    report_path.write_text(original + "\n# TAMPERED\n")

    # Try to cover tracks by editing the lifecycle manifest's report_hash
    # to match the tampered report (and recompute its manifest_hash).
    import hashlib
    lc_path = run_dir / "lifecycle_manifest.json"
    lc_manifest = json.loads(lc_path.read_text())
    h = hashlib.sha256()
    with report_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    lc_manifest["report_hash"] = h.hexdigest()
    lc_manifest.pop("manifest_hash")
    lc_manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(lc_manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lc_path.write_text(json.dumps(lc_manifest, indent=2) + "\n")

    # verify_evidence_manifest must still fail — the SECOND signed
    # evidence.report_bound event's report_hash (written after the lifecycle
    # event) doesn't match the tampered report. The attacker can't forge
    # this because it's in the signed, hash-chained event log.
    assert verify_evidence_manifest(run_dir) is False


# --------------------------------------------------------------------------- #
# Additional: DigestCache unit tests
# --------------------------------------------------------------------------- #


def test_digest_cache_reuses_unchanged_file(tmp_path):
    """DigestCache returns the same hash for an unchanged file without
    re-reading it from disk."""
    from acp.evidence.manifest import DigestCache

    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world\n")

    cache = DigestCache()
    h1 = cache.digest(test_file)
    h2 = cache.digest(test_file)
    assert h1 == h2

    # Verify it's the correct sha256.
    import hashlib
    expected = hashlib.sha256(b"hello world\n").hexdigest()
    assert h1 == expected


def test_digest_cache_recomputes_on_change(tmp_path):
    """DigestCache recomputes the hash when the file changes."""
    from acp.evidence.manifest import DigestCache

    test_file = tmp_path / "test.txt"
    test_file.write_text("original\n")

    cache = DigestCache()
    h1 = cache.digest(test_file)

    # Change the file (need to ensure mtime changes — write + touch).
    test_file.write_text("modified\n")
    import os
    # Force mtime change in case the write is too fast.
    stat = test_file.stat()
    os.utime(test_file, (stat.st_atime, stat.st_mtime + 1))

    h2 = cache.digest(test_file)
    assert h1 != h2


def test_default_ignore_patterns_defined():
    """DEFAULT_IGNORE_PATTERNS contains the expected generated/heavy paths."""
    from acp.evidence.manifest import DEFAULT_IGNORE_PATTERNS

    assert "__pycache__" in DEFAULT_IGNORE_PATTERNS
    assert "node_modules" in DEFAULT_IGNORE_PATTERNS
    assert ".venv" in DEFAULT_IGNORE_PATTERNS
    assert ".git" in DEFAULT_IGNORE_PATTERNS


# --------------------------------------------------------------------------- #
# task.json.status consistency — event log is truth, task.json is projection
# --------------------------------------------------------------------------- #


def test_status_consistency_passed(disposable_repo, isolated_workspace):
    """task.json.status='passed' must match event log with task.completed."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0
    assert "status inconsistent" not in r.output


def test_status_consistency_approved(disposable_repo, isolated_workspace):
    """After approval, task.json.status='approved' must match event log."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 0

    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0
    assert "status inconsistent" not in r.output


def test_status_consistency_rejected(disposable_repo, isolated_workspace):
    """After rejection, task.json.status='archived' must match event log."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    r = runner.invoke(app, [
        "reject", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--rejecter", "test@example.com",
    ])
    assert r.exit_code == 0

    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 0
    assert "status inconsistent" not in r.output


def test_status_inconsistency_detected(disposable_repo, isolated_workspace):
    """If task.json.status lies (doesn't match event log), verify must fail."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # The run passed (task.completed in event log, status='passed').
    # Tamper with task.json to claim a different status.
    task_json_path = run_dir / "task.json"
    data = json.loads(task_json_path.read_text())
    data["status"] = "needs_review"  # lie — event log says passed
    task_json_path.write_text(json.dumps(data, indent=2) + "\n")

    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1
    assert "status inconsistent" in r.output
    assert "event log='passed'" in r.output


def test_status_inconsistency_approved_lie(disposable_repo, isolated_workspace):
    """If task.json claims 'approved' but event log has no human.approved,
    verify must fail."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)

    # The run passed but was NOT approved. Tamper with task.json.
    task_json_path = run_dir / "task.json"
    data = json.loads(task_json_path.read_text())
    data["status"] = "approved"  # lie — no human.approved in event log
    task_json_path.write_text(json.dumps(data, indent=2) + "\n")

    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1
    assert "status inconsistent" in r.output


def test_derive_status_from_events_unit():
    """Unit test for derive_status_from_events."""
    from acp.evidence.manifest import derive_status_from_events
    from acp.models import Event, EventType

    # No terminal events → None.
    events = [
        Event(event_id="evt_000001", task_id="t1", type=EventType.TASK_CREATED, prev_hash="GENESIS", hash="h1"),
    ]
    assert derive_status_from_events(events) is None

    # task.completed → passed.
    events = [
        Event(event_id="evt_000001", task_id="t1", type=EventType.TASK_CREATED, prev_hash="GENESIS", hash="h1"),
        Event(event_id="evt_000002", task_id="t1", type=EventType.TASK_COMPLETED, prev_hash="h1", hash="h2"),
    ]
    assert derive_status_from_events(events) == "passed"

    # human.approved overrides task.completed → approved.
    events = [
        Event(event_id="evt_000001", task_id="t1", type=EventType.TASK_CREATED, prev_hash="GENESIS", hash="h1"),
        Event(event_id="evt_000002", task_id="t1", type=EventType.TASK_COMPLETED, prev_hash="h1", hash="h2"),
        Event(event_id="evt_000003", task_id="t1", type=EventType.HUMAN_APPROVED, prev_hash="h2", hash="h3"),
    ]
    assert derive_status_from_events(events) == "approved"

    # human.rejected overrides everything → rejected.
    events = [
        Event(event_id="evt_000001", task_id="t1", type=EventType.TASK_CREATED, prev_hash="GENESIS", hash="h1"),
        Event(event_id="evt_000002", task_id="t1", type=EventType.TASK_COMPLETED, prev_hash="h1", hash="h2"),
        Event(event_id="evt_000003", task_id="t1", type=EventType.HUMAN_REJECTED, prev_hash="h2", hash="h3"),
    ]
    assert derive_status_from_events(events) == "rejected"

    # task.failed → failed.
    events = [
        Event(event_id="evt_000001", task_id="t1", type=EventType.TASK_CREATED, prev_hash="GENESIS", hash="h1"),
        Event(event_id="evt_000002", task_id="t1", type=EventType.TASK_FAILED, prev_hash="h1", hash="h2"),
    ]
    assert derive_status_from_events(events) == "failed"


# --------------------------------------------------------------------------- #
# evidence_config_hash binding — prevents silent policy downgrade
# --------------------------------------------------------------------------- #


def test_evidence_config_hash_bound_in_finalized(disposable_repo, isolated_workspace):
    """The evidence.finalized event must include evidence_config_hash."""
    task_id, store, run_dir = _run(disposable_repo, isolated_workspace)
    events = _events(store, task_id)

    finalized = [e for e in events if e.type == EventType.EVIDENCE_FINALIZED]
    assert len(finalized) == 1
    assert "evidence_config_hash" in finalized[0].payload
    assert finalized[0].payload["evidence_config_hash"] is not None


def test_evidence_config_tamper_fails_verify(disposable_repo, isolated_workspace, tmp_path):
    """Tampering with evidence_config.json (e.g. downgrading durable_mode)
    must fail acp verify — the config hash in evidence.finalized won't match."""
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.REQUIRED,
    )

    # Verify passes before tampering.
    assert verify_evidence_manifest(run_dir) is True

    # Tamper: downgrade durable_mode from required to best_effort.
    ev_cfg_path = run_dir / "evidence_config.json"
    ev_cfg = json.loads(ev_cfg_path.read_text())
    ev_cfg["durable_mode"] = "best_effort"
    ev_cfg_path.write_text(json.dumps(ev_cfg, indent=2) + "\n")

    # verify_evidence_manifest must fail — config hash doesn't match.
    assert verify_evidence_manifest(run_dir) is False

    # CLI verify must also fail.
    r = runner.invoke(app, ["verify", "--task", task_id,
                            "--runs-root", str(isolated_workspace["runs_root"])])
    assert r.exit_code == 1


def test_evidence_config_durable_store_tamper_fails_verify(disposable_repo, isolated_workspace, tmp_path):
    """Changing the durable_store path in evidence_config.json must fail verify."""
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.REQUIRED,
    )

    assert verify_evidence_manifest(run_dir) is True

    # Tamper: change the durable_store path.
    ev_cfg_path = run_dir / "evidence_config.json"
    ev_cfg = json.loads(ev_cfg_path.read_text())
    ev_cfg["durable_store"] = "/different/path/events.db"
    ev_cfg_path.write_text(json.dumps(ev_cfg, indent=2) + "\n")

    assert verify_evidence_manifest(run_dir) is False
