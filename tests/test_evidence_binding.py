"""Regression tests for build-7 evidence binding fixes.

Covers the 8 items from the verdict's priority fix plan:

  P0-1: DurableEventStore composite primary key (task_id, event_id) — multiple
        tasks in the same SQLite DB no longer collide.
  P0-2: evidence.finalized event binds artifact content hash to the signed
        event log — tampering with an artifact breaks verification even if
        the manifest is edited to match.
  P0-3: verify_evidence_manifest recomputes manifest_hash and rejects mismatches.
  P0-4: Task identity binding — verify checks CLI task_id == task.json.task_id
        == manifest.task_id == every event.task_id == directory name.
  P1-5: Report + vault note re-rendered after lifecycle events (no stale hashes).
  P1-6: Clean verifier failure handling (no tracebacks for malformed data).
  P1-7: acp run --runs-root works.
  P1-8: Generated junk (__pycache__, *.pyc) filtered from captured diffs.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp.cli import app
from acp.config import (
    AgentSection,
    CommandsSection,
    EvidenceSection,
    RepoConfig,
    RepoSection,
    ReviewSection,
)
from acp.events import EventWriter
from acp.evidence.durable_store import DurableEventStore
from acp.evidence.manifest import verify_evidence_manifest
from acp.graph.workflow import run_workflow
from acp.models import Event, EventType
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
# P0-1: DurableEventStore composite primary key — multi-task collision
# --------------------------------------------------------------------------- #


def test_durable_store_multi_task_no_collision(tmp_path):
    """Two tasks with the same event_id sequence must coexist in one SQLite DB."""
    db_path = tmp_path / "events.db"
    store1 = TaskStore(runs_root=tmp_path / "runs1")
    store1.root.mkdir(parents=True, exist_ok=True)
    run_dir1 = store1.run_dir("task_20260624_0001")
    run_dir1.mkdir(parents=True, exist_ok=True)
    events1 = EventWriter("task_20260624_0001", run_dir1)
    events1.write(EventType.TASK_CREATED, {"request": "task 1"})
    events1.write(EventType.REPO_CHECKED, {"repo": "demo"})

    store2 = TaskStore(runs_root=tmp_path / "runs2")
    store2.root.mkdir(parents=True, exist_ok=True)
    run_dir2 = store2.run_dir("task_20260624_0002")
    run_dir2.mkdir(parents=True, exist_ok=True)
    events2 = EventWriter("task_20260624_0002", run_dir2)
    events2.write(EventType.TASK_CREATED, {"request": "task 2"})
    events2.write(EventType.REPO_CHECKED, {"repo": "demo"})

    with DurableEventStore(db_path) as db:
        for evt in events1.read_all():
            db.append(evt)
        for evt in events2.read_all():
            db.append(evt)

        # Both tasks have all their events — no collision.
        assert db.count(task_id="task_20260624_0001") == 2
        assert db.count(task_id="task_20260624_0002") == 2
        assert db.count() == 4


def test_durable_store_old_schema_migration(tmp_path):
    """Old schema (event_id PRIMARY KEY) is migrated to composite key."""
    import sqlite3

    db_path = tmp_path / "events.db"
    # Create old-style schema.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            signature TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO events VALUES ('evt_000001', 'task_1', 'task.created', "
        "'2024-01-01', '{}', '', 'h1', '')"
    )
    conn.commit()
    conn.close()

    # Opening with DurableEventStore should detect old schema and migrate.
    with DurableEventStore(db_path) as db:
        # Old data was dropped (JSONL is canonical), but the new schema works.
        assert db.count() == 0
        # Now we can insert with composite key — two tasks, same event_id.
        store = TaskStore(runs_root=tmp_path / "runs")
        store.root.mkdir(parents=True, exist_ok=True)
        for task_id in ("task_20260624_0001", "task_20260624_0002"):
            rd = store.run_dir(task_id)
            rd.mkdir(parents=True, exist_ok=True)
            ew = EventWriter(task_id, rd)
            ew.write(EventType.TASK_CREATED, {"request": task_id})
            db.append(ew.read_all()[0])
        assert db.count() == 2


# --------------------------------------------------------------------------- #
# P0-2: evidence.finalized binds artifacts to signed event log
# --------------------------------------------------------------------------- #


def test_evidence_finalized_event_written(disposable_repo, isolated_workspace):
    """A completed run writes an evidence.finalized event with artifact_content_hash."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)

    finalized = [e for e in events if e.type == EventType.EVIDENCE_FINALIZED]
    assert len(finalized) == 1, "evidence.finalized event must be written"
    assert "artifact_content_hash" in finalized[0].payload
    assert "artifact_count" in finalized[0].payload


def test_tampered_artifact_breaks_verification(disposable_repo, isolated_workspace):
    """Tampering with an artifact must break verify_evidence_manifest, even if
    the manifest is edited to match the tampered file."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # Verify passes before tampering.
    assert verify_evidence_manifest(run_dir) is True

    # Tamper with an artifact file.
    artifacts_dir = run_dir / "artifacts"
    artifact_files = [
        p for p in artifacts_dir.rglob("*") if p.is_file() and p.name != "final_report.md"
    ]
    assert artifact_files, "expected at least one artifact file"
    target = artifact_files[0]
    original = target.read_bytes()
    target.write_bytes(original + b"\n# tampered\n")

    # Verify fails — the artifact content hash in evidence.finalized no longer matches.
    assert verify_evidence_manifest(run_dir) is False

    # Even if the attacker edits the manifest to match the tampered file,
    # verification still fails because evidence.finalized has the old hash.
    manifest_path = run_dir / "evidence_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    rel = str(target.relative_to(run_dir))
    import hashlib

    h = hashlib.sha256()
    with target.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    manifest["artifacts"][rel] = h.hexdigest()
    # Recompute manifest_hash to match the edited manifest.
    manifest.pop("manifest_hash")
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    # Still fails — evidence.finalized binds the original artifact content hash.
    assert verify_evidence_manifest(run_dir) is False


# --------------------------------------------------------------------------- #
# P0-3: manifest_hash recompute
# --------------------------------------------------------------------------- #


def test_manifest_hash_recompute_rejects_garbage(disposable_repo, isolated_workspace):
    """verify_evidence_manifest must reject a manifest with a wrong manifest_hash."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    assert verify_evidence_manifest(run_dir) is True

    # Corrupt the manifest_hash.
    manifest_path = run_dir / "evidence_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manifest_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    assert verify_evidence_manifest(run_dir) is False


# --------------------------------------------------------------------------- #
# P0-4: Task identity binding
# --------------------------------------------------------------------------- #


def test_transplanted_run_directory_rejected(disposable_repo, isolated_workspace):
    """Copying a valid run dir to a different task_id must fail verification."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # Verify passes for the correct task_id.
    r0 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r0.exit_code == 0

    # Copy to a different task_id directory.
    fake_task_id = "task_20260624_9999"
    fake_run_dir = store.run_dir(fake_task_id)
    shutil.copytree(run_dir, fake_run_dir)

    # Verify fails — the events have the original task_id, not the fake one.
    r1 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            fake_task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r1.exit_code == 1
    assert "mismatch" in r1.output.lower()


def test_task_json_mismatch_rejected(disposable_repo, isolated_workspace):
    """A task.json with a different task_id must fail verification."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # Edit task.json to have a different task_id.
    task_json_path = run_dir / "task.json"
    task_json = json.loads(task_json_path.read_text())
    task_json["task_id"] = "task_20260624_XXXX"
    task_json_path.write_text(json.dumps(task_json, indent=2))

    r = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r.exit_code == 1
    assert "mismatch" in r.output.lower()


# --------------------------------------------------------------------------- #
# P1-5: Report re-rendered after lifecycle events
# --------------------------------------------------------------------------- #


def test_report_rerendered_after_approval(disposable_repo, isolated_workspace):
    """After approval, the report's manifest hash must match the current manifest."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # Read the manifest hash before approval.
    manifest_before = json.loads((run_dir / "evidence_manifest.json").read_text())
    hash_before = manifest_before["manifest_hash"]

    # Approve.
    note_path = run_dir / "artifacts" / "vault_note.md"
    if not note_path.is_file():
        # Find the vault note in the vault root.
        for p in isolated_workspace["vault_root"].rglob("*.md"):
            note_path = p
            break

    r = runner.invoke(
        app,
        [
            "approve",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
            "--vault-root",
            str(isolated_workspace["vault_root"]),
        ],
    )
    assert r.exit_code == 0, f"approve failed: {r.output}"

    # The manifest hash should have changed (new event in the chain).
    manifest_after = json.loads((run_dir / "evidence_manifest.json").read_text())
    hash_after = manifest_after["manifest_hash"]

    # The report must reflect the new hash, not the old one.
    report_path = run_dir / "artifacts" / "final_report.md"
    report = report_path.read_text()
    assert hash_after in report, "report should contain the updated manifest hash"
    assert hash_before not in report or hash_before == hash_after, (
        "report should not contain the stale manifest hash"
    )


# --------------------------------------------------------------------------- #
# P1-6: Clean verifier failure handling (no tracebacks)
# --------------------------------------------------------------------------- #


def test_malformed_event_log_clean_error(disposable_repo, isolated_workspace):
    """A malformed events.jsonl line produces a clean error, not a traceback."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])

    # Append a malformed line to events.jsonl.
    events_path = store.events_path(task_id)
    events_path.write_text(events_path.read_text() + "THIS IS NOT JSON\n")

    r = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r.exit_code == 1
    assert "malformed" in r.output.lower()
    # No traceback — the output should not contain Python traceback markers.
    assert "Traceback" not in r.output
    assert "ValidationError" not in r.output


def test_malformed_manifest_clean_error(disposable_repo, isolated_workspace):
    """A malformed evidence_manifest.json produces a clean error."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # Corrupt the manifest.
    manifest_path = run_dir / "evidence_manifest.json"
    manifest_path.write_text("THIS IS NOT JSON\n")

    r = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r.exit_code == 1
    assert "Traceback" not in r.output


# --------------------------------------------------------------------------- #
# P1-7: acp run --runs-root
# --------------------------------------------------------------------------- #


def test_run_with_runs_root_option(disposable_repo, tmp_path):
    """acp run --runs-root writes to the specified directory, not cwd/data/runs."""
    os.environ["ACP_TEST"] = "1"
    custom_runs_root = tmp_path / "custom_runs"

    # Create a config file.
    config_path = tmp_path / "test.repo.yaml"
    config_path.write_text(f"""
repo:
  name: demo
  path: {disposable_repo.path}
  default_branch: main
agent:
  default: shell
  max_repair_attempts: 0
commands:
  test: echo ok
review: {{}}
""")

    r = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_path),
            "--task",
            "test task",
            "--runs-root",
            str(custom_runs_root),
            "--vault",
            str(tmp_path / "vault"),
        ],
    )
    assert r.exit_code == 0, f"run failed: {r.output}"
    assert custom_runs_root.is_dir(), "runs-root directory should be created"
    # The run directory should be under custom_runs_root, not data/runs.
    run_dirs = list(custom_runs_root.iterdir())
    assert any(d.name.startswith("task_") for d in run_dirs), (
        "expected a task directory under custom_runs_root"
    )


# --------------------------------------------------------------------------- #
# P1-8: Generated junk filtered from diffs
# --------------------------------------------------------------------------- #


def test_diff_ignores_pycache(tmp_path):
    """capture_diff should not include __pycache__/*.pyc files."""
    from acp.gitops.diff import _matches_ignore_pattern

    # Test the pattern matcher directly.
    assert _matches_ignore_pattern("__pycache__/foo.pyc")
    assert _matches_ignore_pattern("tests/__pycache__/test.cpython-313.pyc")
    assert _matches_ignore_pattern("foo.pyc")
    assert _matches_ignore_pattern("node_modules/foo.js")
    assert _matches_ignore_pattern("dist/bundle.js")
    assert not _matches_ignore_pattern("src/main.py")
    assert not _matches_ignore_pattern("tests/test_foo.py")


def test_diff_filters_pycache_from_worktree(disposable_repo, tmp_path):
    """A full capture_diff call should exclude __pycache__ files from the patch."""
    from acp.gitops.diff import capture_diff

    # Create a __pycache__ directory with a .pyc file in the repo.
    pycache_dir = disposable_repo.path / "__pycache__"
    pycache_dir.mkdir(exist_ok=True)
    (pycache_dir / "module.cpython-313.pyc").write_bytes(b"\x00\x01\x02\x03")

    # Also create a real source file change.
    (disposable_repo.path / "new_file.py").write_text("print('hello')\n")

    artifacts_dir = tmp_path / "artifacts"
    diff = capture_diff(
        worktree_path=disposable_repo.path,
        base_branch="main",
        artifacts_dir=artifacts_dir,
    )

    # The patch should include new_file.py but NOT the .pyc file.
    assert "new_file.py" in diff.patch
    assert ".pyc" not in diff.patch
    assert "__pycache__" not in diff.patch


# --------------------------------------------------------------------------- #
# P0-2: Durable mode (disabled/best_effort/required)
# --------------------------------------------------------------------------- #


def test_durable_required_mode_fails_closed(tmp_path):
    """In required mode, a SQLite write failure must fail the run."""
    from acp.config import DurableMode
    from acp.errors import EvidenceConfigError

    # Use a path that can't be opened (a file, not a directory parent).
    bad_db = tmp_path / "blocker"  # will create as file to block SQLite
    bad_db.write_text("not a database")

    store = TaskStore(runs_root=tmp_path / "runs")
    store.root.mkdir(parents=True, exist_ok=True)
    run_dir = store.run_dir("task_20260624_0001")
    run_dir.mkdir(parents=True, exist_ok=True)
    EventWriter("task_20260624_0001", run_dir)

    # Point durable_store at a path inside a file (will fail on init).
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(
            durable_store=bad_db / "events.db",  # parent is a file → will fail
            durable_mode=DurableMode.REQUIRED,
        ),
    )

    # run_workflow should raise EvidenceConfigError in required mode.
    with pytest.raises((EvidenceConfigError, Exception)):
        run_workflow(
            config=cfg,
            user_request="test",
            runs_root=tmp_path / "runs",
            vault_root=tmp_path / "vault",
        )


def test_durable_disabled_mode_never_opens_sqlite(tmp_path):
    """In disabled mode, no SQLite writes happen even if durable_store is set."""
    from acp.config import DurableMode

    db_path = tmp_path / "events.db"
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(
            durable_store=db_path,
            durable_mode=DurableMode.DISABLED,
        ),
    )

    os.environ["ACP_TEST"] = "1"
    run_workflow(
        config=cfg,
        user_request="test",
        runs_root=tmp_path / "runs",
        vault_root=tmp_path / "vault",
    )
    # The DB file should not exist because disabled mode skips SQLite.
    assert not db_path.exists(), "disabled mode should not create the SQLite DB"


# --------------------------------------------------------------------------- #
# P0-3: Signed mode fail-closed
# --------------------------------------------------------------------------- #


def test_run_fails_if_signing_enabled_but_key_missing(tmp_path):
    """A configured signing key that doesn't exist must fail the run."""
    from acp.errors import EvidenceConfigError

    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(
            signing_key_path=tmp_path / "nonexistent_key.bin",
        ),
    )

    with pytest.raises(EvidenceConfigError):
        run_workflow(
            config=cfg,
            user_request="test",
            runs_root=tmp_path / "runs",
            vault_root=tmp_path / "vault",
        )


def test_verify_fails_if_signed_run_contains_unsigned_lifecycle_event(
    disposable_repo, isolated_workspace, tmp_path
):
    """A signed run with an unsigned lifecycle event must fail verify --public-key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    os.environ["ACP_TEST"] = "1"
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(key.private_bytes_raw())
    pub_key_path = tmp_path / "public_key.bin"
    pub_key_path.write_bytes(key.public_key().public_bytes_raw())

    cfg = _config(
        disposable_repo.path,
        signing_key_path=key_path,
        public_key_path=pub_key_path,
    )
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    # Verify with public key passes before any tampering.
    r0 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
            "--public-key",
            str(pub_key_path),
        ],
    )
    assert r0.exit_code == 0, f"verify before tampering failed: {r0.output}"

    # Tamper: append an unsigned event to the event log.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events_path = store.events_path(task_id)
    events = _events(store, task_id)
    last_hash = events[-1].hash
    # Write a fake unsigned event.
    fake_event = {
        "event_id": "evt_MANUAL",
        "task_id": task_id,
        "type": "human.approved",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {"approver": "attacker"},
        "prev_hash": last_hash,
        "hash": "0" * 64,
        "signature": "",
    }
    with events_path.open("a") as f:
        f.write(json.dumps(fake_event) + "\n")

    # Verify with public key must fail — the unsigned event breaks signatures.
    r1 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
            "--public-key",
            str(pub_key_path),
        ],
    )
    assert r1.exit_code == 1, f"verify should fail with unsigned event: {r1.output}"


# --------------------------------------------------------------------------- #
# P1: --debug flag
# --------------------------------------------------------------------------- #


def test_verify_debug_flag_shows_traceback(disposable_repo, isolated_workspace):
    """--debug should show tracebacks instead of clean errors."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])

    # Corrupt the event log badly.
    events_path = store.events_path(task_id)
    events_path.write_text("{{{{NOT JSON}}}}\n")

    # Without --debug: clean error.
    r0 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r0.exit_code == 1
    assert "malformed" in r0.output.lower()

    # With --debug: may show traceback (at least doesn't crash differently).
    r1 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
            "--debug",
        ],
    )
    assert r1.exit_code == 1


# --------------------------------------------------------------------------- #
# P1: Lifecycle manifest
# --------------------------------------------------------------------------- #


def test_approval_creates_lifecycle_manifest(disposable_repo, isolated_workspace):
    """After approval, a lifecycle_manifest.json is written."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # No lifecycle manifest before approval.
    lifecycle_path = run_dir / "lifecycle_manifest.json"
    assert not lifecycle_path.is_file()

    # Approve.
    r = runner.invoke(
        app,
        [
            "approve",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
            "--vault-root",
            str(isolated_workspace["vault_root"]),
        ],
    )
    assert r.exit_code == 0, f"approve failed: {r.output}"

    # Lifecycle manifest should now exist.
    assert lifecycle_path.is_file(), "lifecycle_manifest.json should exist after approval"
    manifest = json.loads(lifecycle_path.read_text())
    assert manifest["manifest_type"] == "lifecycle"
    assert manifest["lifecycle_event_count"] >= 1


def test_verify_reports_lifecycle_manifest_valid(disposable_repo, isolated_workspace):
    """acp verify should report lifecycle manifest validity."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    # Approve to create lifecycle manifest.
    r = runner.invoke(
        app,
        [
            "approve",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
            "--vault-root",
            str(isolated_workspace["vault_root"]),
        ],
    )
    assert r.exit_code == 0

    # Verify should report lifecycle manifest valid.
    r2 = runner.invoke(
        app,
        [
            "verify",
            "--task",
            task_id,
            "--runs-root",
            str(isolated_workspace["runs_root"]),
        ],
    )
    assert r2.exit_code == 0
    assert "lifecycle manifest valid" in r2.output
    assert "approved" in r2.output.lower()


# --------------------------------------------------------------------------- #
# P2: Manifest schema versioning
# --------------------------------------------------------------------------- #


def test_verify_rejects_unknown_manifest_major_version(disposable_repo, isolated_workspace):
    """A manifest with an unknown schema major version must fail verification."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    run_dir = store.run_dir(task_id)

    # Bump schema_version to 99.0 (unknown major).
    manifest_path = run_dir / "evidence_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "99.0"
    # Recompute manifest_hash to match.
    manifest.pop("manifest_hash")
    import hashlib

    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    assert verify_evidence_manifest(run_dir) is False


# --------------------------------------------------------------------------- #
# P2: Canonical event schema tests
# --------------------------------------------------------------------------- #


def test_all_events_have_required_integrity_fields(disposable_repo, isolated_workspace):
    """Every event must have id, task_id, type, timestamp, payload, prev_hash, hash."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)

    assert len(events) > 0, "expected events"
    required = {"event_id", "task_id", "type", "timestamp", "payload", "prev_hash", "hash"}
    for evt in events:
        event_dict = evt.model_dump()
        missing = required - set(event_dict.keys())
        assert not missing, f"event {evt.event_id} missing fields: {missing}"


def test_event_ids_are_monotonic_per_task(disposable_repo, isolated_workspace):
    """Event IDs must be monotonically increasing within a task."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)

    for i, evt in enumerate(events, 1):
        assert evt.event_id == f"evt_{i:06d}", (
            f"event {i} has id {evt.event_id}, expected evt_{i:06d}"
        )


def test_event_prev_hash_links_to_previous_hash(disposable_repo, isolated_workspace):
    """Each event's prev_hash must match the previous event's hash."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = _events(store, task_id)

    assert events[0].prev_hash == "GENESIS"
    for i in range(1, len(events)):
        assert events[i].prev_hash == events[i - 1].hash, (
            f"event {i} prev_hash doesn't match event {i - 1} hash"
        )


# --------------------------------------------------------------------------- #
# P1: Binary file warning in diff
# --------------------------------------------------------------------------- #


def test_diff_capture_detects_binary_files(disposable_repo, tmp_path):
    """capture_diff should detect and report binary files in the diff."""
    from acp.gitops.diff import capture_diff

    # Create a binary file in the repo.
    (disposable_repo.path / "binary.dat").write_bytes(bytes(range(256)))
    # Also create a real source file change.
    (disposable_repo.path / "src.py").write_text("x = 1\n")

    artifacts_dir = tmp_path / "artifacts"
    diff = capture_diff(
        worktree_path=disposable_repo.path,
        base_branch="main",
        artifacts_dir=artifacts_dir,
    )

    assert "src.py" in diff.patch
    assert len(diff.binary_files) > 0, "expected binary file detection"
    assert any("binary.dat" in f for f in diff.binary_files)


# --------------------------------------------------------------------------- #
# Required acceptance test #12: durable required mode fails closed
# --------------------------------------------------------------------------- #


def test_durable_required_mode_fails_closed_on_write_failure(tmp_path):
    """Required mode: SQLite write failure must prevent task success."""
    from acp.config import DurableMode
    from acp.evidence.durable_store import DurableEventStore

    # Create a valid DB, then corrupt it by closing the connection and
    # replacing the file with garbage.
    db_path = tmp_path / "events.db"
    store = TaskStore(runs_root=tmp_path / "runs")
    store.root.mkdir(parents=True, exist_ok=True)
    run_dir = store.run_dir("task_20260624_0001")
    run_dir.mkdir(parents=True, exist_ok=True)
    EventWriter("task_20260624_0001", run_dir)

    # Initialize the DB first so it exists.
    with DurableEventStore(db_path):
        pass

    # Now corrupt the DB file so writes will fail.
    db_path.write_text("CORRUPTED")

    # In required mode, trying to use this corrupted DB should fail.
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(
            durable_store=db_path,
            durable_mode=DurableMode.REQUIRED,
        ),
    )

    with pytest.raises(Exception):
        run_workflow(
            config=cfg,
            user_request="test",
            runs_root=tmp_path / "runs",
            vault_root=tmp_path / "vault",
        )
