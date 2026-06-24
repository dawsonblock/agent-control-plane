"""v0.5.6 tests — config-driven evidence settings + CLI verify/events commands.

Covers:
  - EvidenceSection config parsing (signing_key_path, durable_store, public_key_path)
  - Workflow with signing key enabled produces signed events
  - Workflow with durable store enabled dual-writes to SQLite
  - `acp verify` command on a valid run
  - `acp verify` command on a tampered run
  - `acp verify` with Ed25519 signature verification
  - `acp events` command lists events
  - `acp events` with type filter
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typer.testing import CliRunner

from acp.cli import app
from acp.config import AgentSection, CommandsSection, EvidenceSection, RepoConfig, RepoSection, ReviewSection
from acp.events import verify_event_signatures
from acp.evidence.durable_store import DurableEventStore
from acp.graph.workflow import run_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


runner = CliRunner()


def _config(repo_path: Path, **evidence_kwargs) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test='echo ok'),
        review=ReviewSection(),
        evidence=EvidenceSection(**evidence_kwargs) if evidence_kwargs else EvidenceSection(),
    )


def _main_head(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #


def test_evidence_section_defaults():
    ev = EvidenceSection()
    assert ev.signing_key_path is None
    assert ev.public_key_path is None
    assert ev.durable_store is None


def test_evidence_section_with_values(tmp_path: Path):
    key_path = tmp_path / "key.bin"
    db_path = tmp_path / "events.db"
    ev = EvidenceSection(signing_key_path=key_path, durable_store=db_path)
    assert ev.signing_key_path == key_path.resolve()
    assert ev.durable_store == db_path.resolve()


def test_repo_config_with_evidence_section(tmp_path: Path):
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path),
        evidence=EvidenceSection(durable_store=tmp_path / "events.db"),
    )
    assert cfg.evidence.durable_store == (tmp_path / "events.db").resolve()


# --------------------------------------------------------------------------- #
# Workflow integration: signing
# --------------------------------------------------------------------------- #


def test_workflow_with_signing_key_produces_signed_events(disposable_repo, isolated_workspace, tmp_path):
    """When a signing key is configured, events are Ed25519-signed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    key_path = tmp_path / "signing_key.bin"
    key_path.write_bytes(private_key.private_bytes_raw())

    cfg = _config(disposable_repo.path, signing_key_path=key_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )

    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events_path = store.events_path(result["task_id"])
    from acp.models import Event
    events = [Event.model_validate_json(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert len(events) > 0
    # All events should have non-empty signatures.
    assert all(e.signature for e in events)
    # Signatures should verify against the public key.
    assert verify_event_signatures(events, public_key.public_bytes_raw()) is True


# --------------------------------------------------------------------------- #
# Workflow integration: durable store
# --------------------------------------------------------------------------- #


def test_workflow_with_durable_store_dual_writes(disposable_repo, isolated_workspace, tmp_path):
    """When a durable store is configured, events appear in both JSONL and SQLite."""
    db_path = tmp_path / "events.db"
    cfg = _config(disposable_repo.path, durable_store=db_path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )

    # JSONL has events.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events_path = store.events_path(result["task_id"])
    from acp.models import Event
    jsonl_events = [Event.model_validate_json(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert len(jsonl_events) > 0

    # SQLite has the same events.
    with DurableEventStore(db_path) as db:
        db_events = db.query(task_id=result["task_id"])
        assert len(db_events) == len(jsonl_events)
        # Event types match.
        jsonl_types = [e.type.value for e in jsonl_events]
        db_types = [e.type.value for e in db_events]
        assert jsonl_types == db_types


# --------------------------------------------------------------------------- #
# CLI: acp verify
# --------------------------------------------------------------------------- #


def test_cli_verify_valid_run(disposable_repo, isolated_workspace):
    """`acp verify` on a valid run passes."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r.exit_code == 0, f"verify failed: {r.output}"
    assert "event chain valid" in r.output
    assert "evidence manifest valid" in r.output


def test_cli_verify_tampered_run(disposable_repo, isolated_workspace):
    """`acp verify` on a tampered run fails."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    # Tamper with the event log.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events_path = store.events_path(task_id)
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    events[0]["payload"]["tampered"] = True
    events[0]["hash"] = "wrong"
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r.exit_code == 1, f"verify should have failed: {r.output}"
    assert "INVALID" in r.output


def test_cli_verify_with_signatures(disposable_repo, isolated_workspace, tmp_path):
    """`acp verify --public-key` checks Ed25519 signatures."""
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

    r = runner.invoke(app, [
        "verify", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--public-key", str(pub_path),
    ])
    assert r.exit_code == 0, f"verify with signatures failed: {r.output}"
    assert "Ed25519 signatures valid" in r.output


# --------------------------------------------------------------------------- #
# CLI: acp events
# --------------------------------------------------------------------------- #


def test_cli_events_lists_all_events(disposable_repo, isolated_workspace):
    """`acp events` lists all events from a run."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    r = runner.invoke(app, [
        "events", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r.exit_code == 0, f"events failed: {r.output}"
    assert "task.created" in r.output
    assert "task.completed" in r.output


def test_cli_events_with_type_filter(disposable_repo, isolated_workspace):
    """`acp events --type` filters by event type."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=isolated_workspace["vault_root"],
    )
    task_id = result["task_id"]

    r = runner.invoke(app, [
        "events", "--task", task_id,
        "--runs-root", str(isolated_workspace["runs_root"]),
        "--type", "task.completed",
    ])
    assert r.exit_code == 0, f"events filter failed: {r.output}"
    assert "task.completed" in r.output
    assert "task.created" not in r.output


def test_cli_events_nonexistent_task(isolated_workspace):
    """`acp events` on a non-existent (but valid-shaped) task fails."""
    r = runner.invoke(app, [
        "events", "--task", "task_20260624_9999",
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r.exit_code == 1
    assert "not found" in r.output


def test_cli_events_invalid_task_id_rejected(isolated_workspace):
    """Invalid (path-shaped) task ids are rejected before any filesystem access."""
    r = runner.invoke(app, [
        "events", "--task", "../etc/passwd",
        "--runs-root", str(isolated_workspace["runs_root"]),
    ])
    assert r.exit_code == 1
    assert "invalid task id" in r.output
