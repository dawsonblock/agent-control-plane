"""Worktree management — the heart of ACP's isolation model.

A task runs entirely inside a linked worktree on its own branch. The repo's
default branch is never checked out by ACP, never committed to, and its HEAD
must be identical before and after a run (verified by the caller / tests).
"""

from __future__ import annotations

from pathlib import Path

from git import Repo

from acp.gitops.branches import create_branch


def is_clean(repo_path: Path) -> bool:
    """True if the working tree has no uncommitted changes."""
    repo = Repo(str(repo_path))
    return not repo.is_dirty(untracked_files=True)


def create_worktree(
    repo_path: Path,
    base_branch: str,
    branch_name: str,
    target_path: Path,
) -> tuple[Path, str]:
    """Create branch ``branch_name`` from ``base_branch`` + a linked worktree.

    Fails fast if the repo is dirty — we never want to base agent work on
    uncommitted human changes (see docs/safety.md, worktree-safety rule).

    Returns ``(worktree_path, base_commit_sha)``.
    """
    if not is_clean(repo_path):
        raise RuntimeError(
            f"repo is dirty; refusing to create worktree: {repo_path}"
        )
    target_path = target_path.resolve()
    if target_path.exists():
        raise FileExistsError(f"worktree target already exists: {target_path}")

    base_sha = create_branch(repo_path, base_branch, branch_name)
    repo = Repo(str(repo_path))
    repo.git.worktree("add", str(target_path), branch_name)
    return target_path, base_sha


def remove_worktree(repo_path: Path, worktree_path: Path, force: bool = False) -> None:
    """Remove a linked worktree. Idempotent."""
    repo = Repo(str(repo_path))
    try:
        repo.git.worktree("remove", str(worktree_path), force=force)
    except Exception:
        # Fall back to pruning if the dir was already removed out-of-band.
        repo.git.worktree("prune")
