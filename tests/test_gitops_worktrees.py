"""Tests for acp.gitops.worktrees and acp.gitops.branches."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acp.gitops.branches import create_branch, delete_branch
from acp.gitops.worktrees import (
    create_worktree,
    is_clean,
    remove_worktree,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _branch_exists(repo: Path, name: str) -> bool:
    out = _git(repo, "branch", "--list", name).strip()
    return bool(out)


# ---------------------------------------------------------------------------
# is_clean
# ---------------------------------------------------------------------------


def test_is_clean_true(disposable_repo):
    assert is_clean(disposable_repo.path) is True


def test_is_clean_false(disposable_repo):
    repo_path = disposable_repo.path
    (repo_path / "README.md").write_text("# dirty\n")
    assert is_clean(repo_path) is False


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------


def test_create_branch_success(disposable_repo):
    repo_path = disposable_repo.path
    sha = create_branch(repo_path, "main", "agent/branch-ok")
    assert _branch_exists(repo_path, "agent/branch-ok")
    assert len(sha) == 40
    assert sha == _git(repo_path, "rev-parse", "main").strip()


def test_create_branch_base_not_found(disposable_repo):
    with pytest.raises(ValueError, match="base branch not found"):
        create_branch(disposable_repo.path, "no-such-base", "agent/x")


def test_create_branch_already_exists(disposable_repo):
    repo_path = disposable_repo.path
    create_branch(repo_path, "main", "agent/dup")
    with pytest.raises(FileExistsError, match="branch already exists"):
        create_branch(repo_path, "main", "agent/dup")


# ---------------------------------------------------------------------------
# delete_branch
# ---------------------------------------------------------------------------


def test_delete_branch_success(disposable_repo):
    repo_path = disposable_repo.path
    create_branch(repo_path, "main", "agent/del-me")
    assert _branch_exists(repo_path, "agent/del-me")
    delete_branch(repo_path, "agent/del-me")
    assert not _branch_exists(repo_path, "agent/del-me")


def test_delete_branch_idempotent(disposable_repo):
    # Deleting a non-existent branch is a no-op.
    delete_branch(disposable_repo.path, "agent/never-existed")


def test_delete_branch_refuses_main(disposable_repo):
    with pytest.raises(ValueError, match="refusing to delete default branch"):
        delete_branch(disposable_repo.path, "main")


def test_delete_branch_refuses_master(disposable_repo):
    with pytest.raises(ValueError, match="refusing to delete default branch"):
        delete_branch(disposable_repo.path, "master")


# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_success(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    target = tmp_path / "wt"
    wt_path, base_sha = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/wt-ok",
        target_path=target,
    )
    assert wt_path.exists()
    assert (wt_path / "README.md").is_file()
    assert len(base_sha) == 40
    assert _branch_exists(repo_path, "agent/wt-ok")


def test_create_worktree_dirty_repo(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    (repo_path / "README.md").write_text("# dirty\n")
    with pytest.raises(RuntimeError, match="repo is dirty"):
        create_worktree(
            repo_path=repo_path,
            base_branch="main",
            branch_name="agent/wt-dirty",
            target_path=tmp_path / "wt",
        )


def test_create_worktree_target_exists(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    target = tmp_path / "wt"
    target.mkdir()
    with pytest.raises(FileExistsError, match="worktree target already exists"):
        create_worktree(
            repo_path=repo_path,
            base_branch="main",
            branch_name="agent/wt-exists",
            target_path=target,
        )


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------


def test_remove_worktree_success(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    target = tmp_path / "wt"
    wt_path, _ = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/wt-rm",
        target_path=target,
    )
    assert wt_path.exists()
    remove_worktree(repo_path, wt_path)
    assert not wt_path.exists()


def test_remove_worktree_force(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    target = tmp_path / "wt"
    wt_path, _ = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/wt-force",
        target_path=target,
    )
    # Leave an untracked file in the worktree.
    (wt_path / "untracked.txt").write_text("junk\n")
    remove_worktree(repo_path, wt_path, force=True)
    assert not wt_path.exists()


def test_remove_worktree_idempotent(disposable_repo, tmp_path):
    # Removing a non-existent worktree path should not raise.
    remove_worktree(disposable_repo.path, tmp_path / "never-existed")
