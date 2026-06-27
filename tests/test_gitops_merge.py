"""Tests for acp.gitops.merge — merge_to_base and can_fast_forward."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acp.gitops.branches import create_branch
from acp.gitops.merge import can_fast_forward, merge_to_base


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "commit", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def test_merge_to_base_success(disposable_repo):
    repo_path = disposable_repo.path
    base_before = _git(repo_path, "rev-parse", "main").strip()

    # Create a task branch with a commit.
    create_branch(repo_path, "main", "agent/merge-ok")
    _git(repo_path, "checkout", "-q", "agent/merge-ok")
    task_sha = _commit(repo_path, "task change")

    merge_sha = merge_to_base(repo_path, "agent/merge-ok", "main")

    # main advanced (not the same as before).
    assert merge_sha != base_before
    # The merge commit is on main and is a --no-ff merge (two parents).
    parents = _git(repo_path, "rev-list", "--parents", "-n", "1", "main").split()
    assert len(parents) == 3, f"expected merge commit with 2 parents, got {parents}"
    # The task commit is one of the parents.
    assert task_sha in parents


def test_merge_to_base_base_not_found(disposable_repo):
    repo_path = disposable_repo.path
    create_branch(repo_path, "main", "agent/merge-bnf")
    with pytest.raises(RuntimeError, match="base branch"):
        merge_to_base(repo_path, "agent/merge-bnf", "no-such-base")


def test_merge_to_base_task_not_found(disposable_repo):
    repo_path = disposable_repo.path
    with pytest.raises(RuntimeError, match="task branch"):
        merge_to_base(repo_path, "no-such-task", "main")


def test_merge_to_base_conflict(disposable_repo):
    repo_path = disposable_repo.path

    # Create task branch with a change to README.md.
    create_branch(repo_path, "main", "agent/merge-conflict")
    _git(repo_path, "checkout", "-q", "agent/merge-conflict")
    (repo_path / "README.md").write_text("# task side\n")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-q", "-m", "task edit")

    # Conflicting change on main.
    _git(repo_path, "checkout", "-q", "main")
    (repo_path / "README.md").write_text("# base side\n")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-q", "-m", "base edit")

    base_before = _git(repo_path, "rev-parse", "main").strip()
    with pytest.raises(RuntimeError, match="Auto-merge failed"):
        merge_to_base(repo_path, "agent/merge-conflict", "main")

    # main unchanged after abort.
    base_after = _git(repo_path, "rev-parse", "main").strip()
    assert base_after == base_before, "base branch must be unchanged after failed merge"


def test_can_fast_forward_true(disposable_repo):
    repo_path = disposable_repo.path
    create_branch(repo_path, "main", "agent/ff-yes")
    _git(repo_path, "checkout", "-q", "agent/ff-yes")
    _commit(repo_path, "ahead commit")
    assert can_fast_forward(repo_path, "agent/ff-yes", "main") is True


def test_can_fast_forward_false_diverged(disposable_repo):
    repo_path = disposable_repo.path
    create_branch(repo_path, "main", "agent/ff-diverged")
    _git(repo_path, "checkout", "-q", "agent/ff-diverged")
    _commit(repo_path, "task commit")
    # Diverge main.
    _git(repo_path, "checkout", "-q", "main")
    _commit(repo_path, "main commit")
    assert can_fast_forward(repo_path, "agent/ff-diverged", "main") is False


def test_can_fast_forward_false_missing_branch(disposable_repo):
    repo_path = disposable_repo.path
    assert can_fast_forward(repo_path, "no-such-task", "main") is False
    assert can_fast_forward(repo_path, "main", "no-such-base") is False
