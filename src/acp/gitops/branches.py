"""Branch operations — create/delete the per-task ``agent/<task_id>`` branch.

Never operates on the repo's default branch. Branch names are derived from
task ids so collisions are impossible by construction (task ids are unique).
"""

from __future__ import annotations

from pathlib import Path

from git import Repo


def create_branch(repo_path: Path, base_branch: str, branch_name: str) -> str:
    """Create ``branch_name`` at the current tip of ``base_branch``.

    Returns the commit sha the branch points at. Raises if ``base_branch``
    doesn't exist or ``branch_name`` already exists.
    """
    repo = Repo(str(repo_path))
    if base_branch not in [h.name for h in repo.heads]:
        raise ValueError(f"base branch not found: {base_branch}")
    if branch_name in [h.name for h in repo.heads]:
        raise FileExistsError(f"branch already exists: {branch_name}")
    base_commit = repo.heads[base_branch].commit
    repo.create_head(branch_name, commit=base_commit)
    return base_commit.hexsha


def delete_branch(repo_path: Path, branch_name: str, force: bool = False) -> None:
    """Delete a task branch. Refuses the default branch even with force."""
    if branch_name in ("main", "master"):
        raise ValueError(f"refusing to delete default branch: {branch_name}")
    repo = Repo(str(repo_path))
    if branch_name not in [h.name for h in repo.heads]:
        return  # already gone — idempotent
    repo.delete_head(branch_name, force=force)
