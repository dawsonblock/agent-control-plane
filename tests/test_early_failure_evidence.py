"""v0.5.5 tests — early-failure evidence and terminal event correctness.

When a task fails early (dirty repo, worktree creation error, node crash):
  - exactly ONE terminal event (task.failed) is written — no duplicates
  - a minimal failure report is written even without a diff/review
  - the evidence manifest is written

When a task fails mid-run (failing test, no repair):
  - exactly ONE terminal event
  - a full report is written (with diff + review)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _config(repo_path: Path, *, test_cmd='echo ok', max_repair=0) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=max_repair),
        commands=CommandsSection(test=test_cmd),
        review=ReviewSection(),
    )


def _run_graph(repo_path, runs_root, vault_root, *, cfg=None):
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)
    if cfg is None:
        cfg = _config(repo_path)
    state = initial_state(
        config=cfg, user_request="test", vault_root=vault_root, runs_root=runs_root,
    )
    return wf.invoke(state, config={"configurable": {"thread_id": "test"}}), store


def _events(store, task_id):
    p = store.events_path(task_id)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _main_head(repo_path):
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# Dirty repo — early failure
# --------------------------------------------------------------------------- #


def test_dirty_repo_exactly_one_terminal_event(disposable_repo, isolated_workspace):
    """Dirty repo → exactly one task.failed event, no duplicates."""
    (disposable_repo.path / "README.md").write_text("# dirty\n")

    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
    )

    assert result["status"] == TaskStatus.FAILED
    events = _events(store, result["task_id"])
    terminal = [e for e in events if e["type"] == EventType.TASK_FAILED.value]
    assert len(terminal) == 1, (
        f"expected exactly 1 task.failed event, got {len(terminal)}: "
        f"{[e['type'] for e in events]}"
    )


def test_dirty_repo_writes_minimal_failure_report(disposable_repo, isolated_workspace):
    """Dirty repo → a failure report is written even without a diff."""
    (disposable_repo.path / "README.md").write_text("# dirty\n")

    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
    )

    report_path = Path(str(result.get("report_path", "")))
    assert report_path.is_file(), "failure report should exist even for early failure"
    body = report_path.read_text()
    assert "failed" in body.lower()
    assert "Failure" in body  # the failure section


def test_dirty_repo_writes_evidence_manifest(disposable_repo, isolated_workspace):
    """Dirty repo → evidence manifest is written."""
    (disposable_repo.path / "README.md").write_text("# dirty\n")

    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
    )

    manifest_path = store.run_dir(result["task_id"]) / "evidence_manifest.json"
    assert manifest_path.is_file(), "evidence manifest should exist for early failure"


def test_dirty_repo_main_untouched(disposable_repo, isolated_workspace):
    """Dirty repo → main branch HEAD unchanged."""
    (disposable_repo.path / "README.md").write_text("# dirty\n")
    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
    )
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


# --------------------------------------------------------------------------- #
# Worktree creation failure — early failure
# --------------------------------------------------------------------------- #


def test_worktree_failure_exactly_one_terminal_event(disposable_repo, isolated_workspace):
    """Worktree creation failure (bad branch) → exactly one task.failed."""
    # Use a non-existent default branch to force worktree creation to fail.
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=disposable_repo.path, default_branch="nonexistent"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
    )

    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        cfg=cfg,
    )

    assert result["status"] == TaskStatus.FAILED
    events = _events(store, result["task_id"])
    terminal = [e for e in events if e["type"] == EventType.TASK_FAILED.value]
    assert len(terminal) == 1, (
        f"expected exactly 1 task.failed event, got {len(terminal)}: "
        f"{[e['type'] for e in events]}"
    )
    # A node.failed event was written for the worktree failure.
    node_failed = [e for e in events if e["type"] == EventType.NODE_FAILED.value]
    assert any("worktree" in e["payload"].get("node", "") for e in node_failed)


# --------------------------------------------------------------------------- #
# Mid-run failure — full report
# --------------------------------------------------------------------------- #


def test_mid_run_failure_exactly_one_terminal_event(disposable_repo, isolated_workspace):
    """Failing test (no repair) → exactly one task.failed, full report."""
    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        cfg=_config(disposable_repo.path, test_cmd="exit 1", max_repair=0),
    )

    assert result["status"] == TaskStatus.FAILED
    events = _events(store, result["task_id"])
    terminal = [e for e in events if e["type"] == EventType.TASK_FAILED.value]
    assert len(terminal) == 1

    # Full report with diff + review.
    report_path = Path(str(result["report_path"]))
    assert report_path.is_file()
    body = report_path.read_text()
    assert "Gate Summary" in body
