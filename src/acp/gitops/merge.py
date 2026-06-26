"""Git merge operations for autonomous mode (v0.6.0).

Merges a task branch (``agent/<task_id>``) into the default branch after
auto-approval. The merge is a clean ``--no-ff`` merge — this preserves the
task's commit history in the merge commit, making the evidence trail
traceable from the default branch back to the task branch.

If the merge fails (conflicts, diverged base, etc.), the merge is aborted
and the task branch is left intact for manual resolution.
"""

from __future__ import annotations

from pathlib import Path

from git import GitCommandError, Repo


def merge_to_base(
    repo_path: Path,
    task_branch: str,
    base_branch: str,
) -> str:
    """Merge ``task_branch`` into ``base_branch`` with a merge commit.

    Performs a ``--no-ff`` merge (always creates a merge commit, even if
    a fast-forward is possible) so the task branch's history is preserved
    in the default branch's log.

    Returns the SHA of the merge commit.

    Raises ``RuntimeError`` if the merge fails (conflicts, branch not
    found, etc.). On failure, the merge is aborted and the repo is left
    on the task branch — the base branch is not modified.
    """
    repo = Repo(str(repo_path))

    # Verify both branches exist.
    branch_names = [h.name for h in repo.heads]
    if base_branch not in branch_names:
        raise RuntimeError(
            f"base branch '{base_branch}' not found in repo"
        )
    if task_branch not in branch_names:
        raise RuntimeError(
            f"task branch '{task_branch}' not found in repo"
        )

    # Save the current branch so we can restore on failure.
    original_branch = repo.active_branch.name

    try:
        # Checkout the base branch.
        repo.git.checkout(base_branch)

        # Attempt a clean merge (no fast-forward to preserve history).
        repo.git.merge(
            "--no-ff",
            "-m",
            f"Auto-merge ACP task {task_branch}",
            task_branch,
        )
        return repo.head.commit.hexsha
    except GitCommandError as exc:
        # If merge conflict occurs, abort and rollback.
        try:
            repo.git.merge("--abort")
        except GitCommandError:
            pass  # merge may not have started
        # Restore the original branch.
        try:
            repo.git.checkout(original_branch)
        except GitCommandError:
            pass
        raise RuntimeError(
            f"Auto-merge failed for task '{task_branch}': {exc}"
        ) from exc


def can_fast_forward(
    repo_path: Path,
    task_branch: str,
    base_branch: str,
) -> bool:
    """Check if ``task_branch`` can be fast-forwarded to ``base_branch``.

    Returns True if the base branch is an ancestor of the task branch
    (i.e., the task branch is strictly ahead of base with no divergence).
    This is a quick pre-check before attempting a merge.
    """
    repo = Repo(str(repo_path))
    try:
        base_commit = repo.commit(base_branch)
        task_commit = repo.commit(task_branch)
        return repo.git.merge_base(
            "--is-ancestor", base_commit.hexsha, task_commit.hexsha
        ) == ""
    except (GitCommandError, ValueError):
        return False
