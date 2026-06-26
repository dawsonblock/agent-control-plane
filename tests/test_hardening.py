"""Tests for edge cases and hardening added in self-review round 3.

Covers:
  - agent.run() returning None → RuntimeError, not AttributeError
  - empty event log fails verify_event_chain and verify_event_signatures
  - verify_evidence_manifest with missing/empty events.jsonl fails
  - verify_evidence_manifest with missing manifest fails
  - vault note path traversal rejected
  - run_workflow creates vault_root if it doesn't exist
  - events --type filter validates against EventType enum
  - list command warns on malformed task.json
  - set_signing_key validates key length before cryptography call
  - build_evidence_manifest with empty events marks chain invalid
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp.cli import app
from acp.config import AgentSection, CommandsSection, EvidenceSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter, verify_event_chain, verify_event_signatures
from acp.evidence.manifest import build_evidence_manifest, verify_evidence_manifest
from acp.graph.workflow import run_workflow
from acp.models import EventType, Task, TaskStatus
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


# --------------------------------------------------------------------------- #
# Empty event log fails verification
# --------------------------------------------------------------------------- #


def test_verify_event_chain_empty_list_fails():
    """An empty event log has no evidence trail — verification must fail."""
    assert verify_event_chain([]) is False


def test_verify_event_signatures_empty_list_fails():
    """An empty event log should not pass signature verification."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    assert verify_event_signatures([], pub) is False


def test_verify_event_chain_single_event_valid(tmp_path):
    """A single event with prev_hash=GENESIS is valid."""
    w = EventWriter("task_20260624_0001", tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    events = w.read_all()
    assert len(events) == 1
    assert verify_event_chain(events) is True


# --------------------------------------------------------------------------- #
# verify_evidence_manifest edge cases
# --------------------------------------------------------------------------- #


def test_verify_evidence_manifest_missing_manifest_fails(tmp_path):
    """No manifest file → verification fails."""
    assert verify_evidence_manifest(tmp_path) is False


def test_verify_evidence_manifest_missing_events_jsonl_fails(tmp_path):
    """Manifest exists but events.jsonl is missing → fails (event log is source of truth)."""
    run_dir = tmp_path / "task_20260624_0001"
    run_dir.mkdir()
    (run_dir / "artifacts").mkdir()
    manifest = {
        "task_id": "task_20260624_0001",
        "event_count": 1,
        "event_chain_head": "abc123",
        "event_chain_valid": True,
        "artifacts": {},
        "manifest_hash": "fake",
    }
    (run_dir / "evidence_manifest.json").write_text(json.dumps(manifest))
    assert verify_evidence_manifest(run_dir) is False


def test_verify_evidence_manifest_empty_events_jsonl_fails(tmp_path):
    """Manifest exists, events.jsonl exists but is empty → fails."""
    run_dir = tmp_path / "task_20260624_0001"
    run_dir.mkdir()
    (run_dir / "artifacts").mkdir()
    (run_dir / "events.jsonl").write_text("")
    manifest = {
        "task_id": "task_20260624_0001",
        "event_count": 0,
        "event_chain_head": "",
        "event_chain_valid": True,
        "artifacts": {},
        "manifest_hash": "fake",
    }
    (run_dir / "evidence_manifest.json").write_text(json.dumps(manifest))
    assert verify_evidence_manifest(run_dir) is False


def test_build_evidence_manifest_empty_events_marks_chain_invalid(tmp_path):
    """build_evidence_manifest with zero events sets event_chain_valid=False."""
    run_dir = tmp_path / "task_20260624_0001"
    run_dir.mkdir()
    (run_dir / "artifacts").mkdir()
    w = EventWriter("task_20260624_0001", run_dir)
    # No events written
    manifest = build_evidence_manifest(run_dir=run_dir, events_writer=w)
    assert manifest["event_chain_valid"] is False
    assert manifest["event_count"] == 0


# --------------------------------------------------------------------------- #
# set_signing_key validates key length
# --------------------------------------------------------------------------- #


def test_set_signing_key_rejects_wrong_length(tmp_path):
    """set_signing_key validates key length before calling cryptography."""
    w = EventWriter("task_20260624_0001", tmp_path)
    with pytest.raises(ValueError, match="32 bytes"):
        w.set_signing_key(b"only-ten-")  # 9 bytes
    with pytest.raises(ValueError, match="32 bytes"):
        w.set_signing_key(b"\x00" * 64)  # 64 bytes


def test_set_signing_key_accepts_valid_length(tmp_path):
    """set_signing_key accepts a 32-byte key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate().private_bytes_raw()
    w = EventWriter("task_20260624_0001", tmp_path)
    w.set_signing_key(key)  # should not raise


# --------------------------------------------------------------------------- #
# Vault note path traversal
# --------------------------------------------------------------------------- #


def test_write_vault_note_rejects_path_traversal_in_task_id(tmp_path):
    """A task_id with path separators is rejected — no path traversal."""
    from acp.gitops.diff import DiffCapture
    from acp.models import Recommendation, RiskLevel, ReviewResult
    from acp.vault.obsidian_writer import write_vault_note

    task = Task(
        task_id="../etc/passwd",  # path traversal attempt
        task_branch="agent/evil",
        repo_name="demo",
        repo_path=str(tmp_path),
        base_branch="main",
        user_request="malicious",
        base_commit_sha="abc",
        worktree_path=tmp_path / "wt",
    )
    review = ReviewResult(
        risk=RiskLevel.LOW,
        recommendation=Recommendation.MERGE,
        concerns=[],
    )
    diff = DiffCapture(
        patch="",
        stat="",
        changed_files=[],
        insertions=0,
        deletions=0,
    )
    with pytest.raises(ValueError, match="path separators"):
        write_vault_note(
            report_body="# test",
            task=task,
            review=review,
            diff=diff,
            vault_root=tmp_path / "vault",
        )


# --------------------------------------------------------------------------- #
# run_workflow creates vault_root if it doesn't exist
# --------------------------------------------------------------------------- #


def test_run_workflow_creates_vault_root_if_missing(disposable_repo, isolated_workspace):
    """run_workflow creates vault_root if it doesn't exist — no crash."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)
    vault_root = isolated_workspace["vault_root"]
    # vault_root should already exist from the fixture, but verify the workflow
    # doesn't crash if we point to a subdirectory that doesn't exist yet.
    new_vault = vault_root / "subdir" / "deeper"
    assert not new_vault.exists()
    result = run_workflow(
        config=cfg,
        user_request="test task",
        runs_root=isolated_workspace["runs_root"],
        vault_root=new_vault,
    )
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)
    assert new_vault.is_dir(), "vault_root should have been created"
    assert (new_vault / "tasks" / f"{result['task_id']}.md").is_file()


# --------------------------------------------------------------------------- #
# events --type filter validates against EventType enum
# --------------------------------------------------------------------------- #


def test_events_command_rejects_invalid_type_filter(disposable_repo, isolated_workspace):
    """events --type with an unknown event type gives a clean error, not empty output."""
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
        "--type", "bogus.event_type",
    ])
    assert r.exit_code == 1
    assert "unknown event type" in r.output
    assert "bogus.event_type" in r.output


def test_events_command_valid_type_filter_works(disposable_repo, isolated_workspace):
    """events --type with a valid event type filters correctly."""
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
        "--type", "task.created",
    ])
    assert r.exit_code == 0
    assert "task.created" in r.output
    assert "task.completed" not in r.output


# --------------------------------------------------------------------------- #
# list command warns on malformed task.json
# --------------------------------------------------------------------------- #


def test_list_command_warns_on_malformed_task_json(tmp_path):
    """list warns when it encounters malformed task.json files."""
    runs_root = tmp_path / "runs"
    # Create a valid task.
    task_dir = runs_root / "task_20260624_0001"
    task_dir.mkdir(parents=True)
    task = Task(
        task_id="task_20260624_0001",
        task_branch="agent/task_20260624_0001",
        repo_name="demo",
        repo_path=str(tmp_path),
        base_branch="main",
        user_request="test",
        base_commit_sha="abc",
        worktree_path=tmp_path / "wt",
    )
    (task_dir / "task.json").write_text(task.model_dump_json())

    # Create a malformed task.json.
    bad_dir = runs_root / "task_20260624_0002"
    bad_dir.mkdir()
    (bad_dir / "task.json").write_text("{ not valid json")

    r = runner.invoke(app, ["list", "--runs-root", str(runs_root)])
    assert r.exit_code == 0
    assert "task_20260624_0001" in r.output
    assert "skipped" in r.output
    assert "malformed" in r.output


# --------------------------------------------------------------------------- #
# agent.run() returning None → RuntimeError
# --------------------------------------------------------------------------- #


def test_agent_returning_none_raises_runtime_error(disposable_repo, isolated_workspace):
    """If agent.run() returns None, the graph raises RuntimeError, not AttributeError."""
    os.environ["ACP_TEST"] = "1"
    cfg = _config(disposable_repo.path)

    # Build a custom agent factory that returns an agent whose run() returns None.
    from acp.agents.base import AgentProtocol
    class NoneAgent(AgentProtocol):
        name = "none-agent"
        def run(self, *, prompt_path, worktree_path, artifact_dir, timeout_seconds):
            return None

    from acp.graph.workflow import build_workflow
    runs_root = isolated_workspace["runs_root"]
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events, agent_factory=lambda cfg: NoneAgent())

    from acp.graph.state import initial_state
    result = wf.invoke(
        initial_state(config=cfg, user_request="test", vault_root=isolated_workspace["vault_root"], runs_root=runs_root),
        config={"configurable": {"thread_id": "test"}},
    )
    # The graph should catch the error and route to failed_node.
    assert result["status"] == TaskStatus.FAILED
    # The error message should mention "None" or "bug in the agent".
    error = result.get("error", "")
    assert "None" in error or "bug" in error or "agent" in error.lower()


# --------------------------------------------------------------------------- #
# Malformed events.jsonl line doesn't corrupt the hash chain
# --------------------------------------------------------------------------- #


def test_event_writer_read_all_skips_malformed_lines(tmp_path):
    """Malformed lines in events.jsonl are skipped, not crashed on."""
    w = EventWriter("task_20260624_0001", tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})

    # Append a malformed line to the file.
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(events_path.read_text() + "{ malformed json\n")

    # read_all should skip the malformed line.
    events = w.read_all()
    assert len(events) == 2  # only the two valid events
    assert all(e.type in (EventType.TASK_CREATED, EventType.REPO_CHECKED) for e in events)
