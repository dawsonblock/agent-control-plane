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
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp.cli import app
from acp.config import AgentSection, CommandsSection, DurableMode, EvidenceSection, RepoConfig, RepoSection, ReviewSection
from acp.evidence.manifest import (
    compute_report_hash,
    compute_task_json_hash,
    read_evidence_config,
    verify_evidence_manifest,
)
from acp.events import EventWriter
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
    is unavailable — not just warn."""
    # Use a path that will be unreadable after the run.
    db_path = tmp_path / "events.db"
    task_id, store, run_dir = _run(
        disposable_repo, isolated_workspace,
        durable_store=db_path, durable_mode=DurableMode.REQUIRED,
    )

    # Make the durable store path unusable — point the config to a path
    # inside a file (not a directory) so SQLite can't open it.
    ev_cfg_path = run_dir / "evidence_config.json"
    ev_cfg = json.loads(ev_cfg_path.read_text())
    # Point to /dev/null/cannot_exist — a path that can't be a SQLite DB.
    ev_cfg["durable_store"] = str(tmp_path / "blocker_file" / "events.db")
    ev_cfg_path.write_text(json.dumps(ev_cfg, indent=2) + "\n")
    # Create the blocker file so the parent can't be a directory.
    (tmp_path / "blocker_file").write_text("blocker")

    # Approve must fail (not just warn).
    r = runner.invoke(app, [
        "approve", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--vault-root", str(isolated_workspace["vault_root"]),
        "--approver", "test@example.com",
    ])
    assert r.exit_code == 1, f"approve should have failed closed: {r.output}"
    assert "durable store write failed" in r.output
    assert "required" in r.output


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
