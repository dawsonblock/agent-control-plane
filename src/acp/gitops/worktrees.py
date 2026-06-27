"""Worktree management — the heart of ACP's isolation model.

A task runs entirely inside a linked worktree on its own branch. The repo's
default branch is never checked out by ACP, never committed to, and its HEAD
must be identical before and after a run (verified by the caller / tests).

**This is workflow isolation, not a security sandbox.** The worktree
prevents the agent from touching the default branch, but it does NOT
prevent a malicious agent or command from accessing the filesystem, network,
SSH keys, environment variables, home directory, or other repos. True
sandboxing (containers, seccomp, etc.) is a future concern.
"""

from __future__ import annotations

import logging
from pathlib import Path

from git import Repo

from acp.gitops.branches import create_branch

logger = logging.getLogger(__name__)


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
        raise RuntimeError(f"repo is dirty; refusing to create worktree: {repo_path}")
    target_path = target_path.resolve()
    if target_path.exists():
        raise FileExistsError(f"worktree target already exists: {target_path}")

    base_sha = create_branch(repo_path, base_branch, branch_name)
    repo = Repo(str(repo_path))
    repo.git.worktree("add", str(target_path), branch_name)
    return target_path, base_sha


def remove_worktree(repo_path: Path, worktree_path: Path, force: bool = False) -> None:
    """Remove a linked worktree. Idempotent.

    Always prunes worktree metadata afterward — ``git worktree remove`` can
    succeed at removing the working directory but leave stale administrative
    metadata in ``.git/worktrees/``, which causes ``git branch -d`` to refuse
    with "cannot delete branch used by worktree". Pruning cleans that up.

    When ``force=True``, uses ``-f`` to remove worktrees with uncommitted or
    untracked files (agent runs typically leave files behind). Note: GitPython
    translates ``force=True`` to ``--force`` (a global git option), but
    ``git worktree remove`` expects ``-f`` as a subcommand option — so we pass
    it explicitly as a string argument.
    """
    repo = Repo(str(repo_path))
    try:
        if force:
            repo.git.worktree("remove", "-f", str(worktree_path))
        else:
            repo.git.worktree("remove", str(worktree_path))
    except Exception as exc:  # noqa: BLE001
        # The worktree dir may have been removed out-of-band — that's fine.
        # But log it so orphaned worktrees can be debugged if they accumulate.
        logger.warning(
            "worktree remove failed (path=%s, force=%s): %s — continuing with prune",
            worktree_path,
            force,
            exc,
        )
    # Always prune to clean up stale worktree metadata, even after successful
    # removal. Without this, git may still consider the branch "checked out"
    # in a now-removed worktree, blocking branch deletion.
    repo.git.worktree("prune")


def create_worktree_from_ref(
    repo_path: Path,
    ref: str,
    target_path: Path,
) -> Path:
    """Create a linked worktree from an arbitrary ref (e.g. a sandbox remote).

    Used by the ``docker_sbx`` executor backend: after the agent finishes
    inside the sandbox and ACP fetches the sandbox remote, this creates a
    temporary worktree from the remote's branch so the existing test runner
    and diff capture can operate on the agent's actual changes.

    Returns the worktree path.
    """
    target_path = target_path.resolve()
    if target_path.exists():
        raise FileExistsError(f"worktree target already exists: {target_path}")
    repo = Repo(str(repo_path))
    repo.git.worktree("add", str(target_path), ref)
    return target_path
