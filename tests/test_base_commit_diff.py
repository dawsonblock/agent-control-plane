"""Tests for base commit SHA recording and stable diff — v0.5 acceptance.

Requirements:
  - base_commit_sha is recorded when the worktree is created
  - worktree.created event includes base_commit_sha
  - diff is stable (uses recorded SHA, not moving branch name)
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


def _config(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(),
        commands=CommandsSection(lint='echo "lint ok"', test='echo "ok"'),
        review=ReviewSection(),
    )


def _run_graph(
    repo_path: Path,
    runs_root: Path,
    vault_root: Path,
):
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)

    cfg = _config(repo_path)
    state = initial_state(
        config=cfg,
        user_request="test base commit sha",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    result = wf.invoke(state, config={"configurable": {"thread_id": "acp-test"}})
    return result, store


def _event_types_path(store: TaskStore, task_id: str) -> Path:
    return store.events_path(task_id)


def _events(store: TaskStore, task_id: str) -> list[dict]:
    p = _event_types_path(store, task_id)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_base_commit_sha_recorded(disposable_repo, isolated_workspace):
    """Task.base_commit_sha must be non-empty after worktree creation."""
    result, store = _run_graph(
        disposable_repo.path,
        Path(isolated_workspace["runs_root"]) / "sha",
        isolated_workspace["vault_root"],
    )

    task = result["task"]
    assert task.base_commit_sha, f"base_commit_sha should not be empty: {task.base_commit_sha!r}"
    assert len(task.base_commit_sha) == 40, f"expected 40-char SHA, got {len(task.base_commit_sha)}"


def test_worktree_created_event_includes_base_commit_sha(disposable_repo, isolated_workspace):
    """The worktree.created event must carry base_commit_sha."""
    result, store = _run_graph(
        disposable_repo.path,
        Path(isolated_workspace["runs_root"]) / "sha_event",
        isolated_workspace["vault_root"],
    )

    task_id = result["task_id"]
    evts = _events(store, task_id)
    worktree_evts = [e for e in evts if e["type"] == EventType.WORKTREE_CREATED.value]

    assert len(worktree_evts) == 1, "expected exactly one worktree.created event"
    payload = worktree_evts[0]["payload"]
    assert "base_commit_sha" in payload, f"missing base_commit_sha in event payload: {payload}"
    assert len(payload["base_commit_sha"]) == 40, f"expected 40-char SHA, got {payload['base_commit_sha']!r}"


def test_diff_uses_recorded_sha_not_moving_branch(disposable_repo, isolated_workspace):
    """Diff must compare against the recorded base SHA, not a moving branch tip.

    Scenario:
      1. Record original main HEAD (commit A).
      2. Create ACP worktree from commit A.
      3. Advance main to commit B (new empty commit).
      4. Verify diff artifacts exist and reference the correct baseline.
    """
    # Record the original main HEAD (commit A).
    original_head = subprocess.run(
        ["git", "-C", str(disposable_repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    result, store = _run_graph(
        disposable_repo.path,
        Path(isolated_workspace["runs_root"]) / "sha_diff",
        isolated_workspace["vault_root"],
    )

    task = result["task"]
    recorded_sha = task.base_commit_sha

    # The recorded SHA must equal the original HEAD.
    assert recorded_sha == original_head, (
        f"recorded SHA {recorded_sha} does not match original HEAD {original_head}"
    )

    # Advance main with a new commit (commit B).
    subprocess.run(
        ["git", "-C", str(disposable_repo.path), "commit", "--allow-empty", "-m", "advance main past worktree base"],
        capture_output=True, text=True, check=True,
    )
    new_head = subprocess.run(
        ["git", "-C", str(disposable_repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert new_head != recorded_sha, "main must have advanced past the recorded SHA"

    # Diff artifacts must exist and be non-empty.
    artifacts = store.artifacts_dir(result["task_id"])
    diff_patch = artifacts / "diff.patch"
    diff_stat = artifacts / "diff_stat.txt"
    assert diff_patch.is_file(), f"diff.patch missing at {diff_patch}"
    assert diff_stat.is_file(), f"diff_stat.txt missing at {diff_stat}"
    patch_content = diff_patch.read_text()
    assert patch_content.strip(), "diff.patch should not be empty — agent changes exist"

    assert recorded_sha not in patch_content, (
        "SHA should not appear literally inside the diff content"
    )


def test_diff_against_recorded_sha_not_moved_branch_with_conflict(disposable_repo, isolated_workspace):
    """Diff must be computed against the recorded base SHA, not the moved branch tip.

    This test proves the invariant by advancing ``main`` with a conflicting change
    *between* worktree creation and diff capture. If ``capture_diff`` used the
    branch name (``main``) instead of the recorded SHA, the diff would include
    the conflict — proving we use the correct baseline.
    """
    repo_path = disposable_repo.path

    # 1. Record original main HEAD (commit A).
    original_head = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # 2. Create a worktree at commit A via the gitops module.
    from acp.gitops.worktrees import create_worktree

    runs_root = Path(isolated_workspace["runs_root"]) / "sha_conflict"
    runs_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = runs_root / "worktree"

    worktree_path, base_sha = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/test-conflict-diff",
        target_path=worktree_dir,
    )
    assert base_sha == original_head, f"expected {original_head}, got {base_sha}"

    # 3. Advance main with a conflicting change to README.md (commit B).
    #    Both the agent change and commit B will edit README.md — the diff
    #    should only include the agent's change.
    main_readme = repo_path / "README.md"
    b_content = main_readme.read_text() + "\n# B change on main\n"
    main_readme.write_text(b_content)
    subprocess.run(
        ["git", "-C", str(repo_path), "add", "README.md"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", "conflicting change on main"],
        capture_output=True, text=True, check=True,
    )
    new_head = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert new_head != base_sha, "main must have advanced past the recorded SHA"

    # 4. Make a (non-conflicting) change in the worktree.
    wt_readme = worktree_dir / "README.md"
    wt_readme.write_text(wt_readme.read_text() + "\n# Agent change in worktree\n")

    # 5. Run capture_diff with the recorded SHA.
    from acp.gitops.diff import capture_diff

    artifacts_dir = runs_root / "artifacts"
    diff = capture_diff(
        worktree_path=worktree_path,
        base_branch="main",
        artifacts_dir=artifacts_dir,
        base_commit_sha=base_sha,
    )

    # 6. Assert the diff only contains the agent's worktree change, NOT the
    #    conflicting change from commit B. If capture_diff used the branch
    #    name (now pointing at commit B), the diff would include both changes
    #    and show "(no previous index)" or two separate additions.
    assert "Agent change in worktree" in diff.patch, (
        "diff must include the worktree change"
    )
    assert "# B change on main" not in diff.patch, (
        "diff must NOT include commit B's change — proves baseline is "
        f"the recorded SHA {base_sha}, not the moved branch 'main' ({new_head})"
    )

    # 7. Diff is non-empty and contains exactly the worktree change.
    assert "README.md" in diff.stat, (
        "stat output must reference the changed file (README.md)"
    )
    assert diff.changed_files == ["README.md"], (
        f"expected only README.md changed, got {diff.changed_files}"
    )
    assert diff.insertions >= 1, "at least one line added"