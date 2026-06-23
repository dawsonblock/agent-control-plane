"""Diff capture — what the agent changed, frozen as artifacts.

Produces two files: a full unified patch (``diff.patch``) and a condensed
``diff_stat.txt``. The patch is evidence; the stat drives the reviewer's
file-count and line-count heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from git import Repo


@dataclass
class DiffCapture:
    patch: str
    stat: str
    changed_files: list[str]
    insertions: int
    deletions: int


def capture_diff(
    worktree_path: Path,
    base_branch: str,
    artifacts_dir: Path,
    base_commit_sha: str | None = None,
) -> DiffCapture:
    """Diff the worktree's working tree against ``base_branch``.

    If ``base_commit_sha`` is provided, diff against that exact commit rather
    than resolving the branch name (avoids races if the branch moves). Writes
    ``diff.patch`` and ``diff_stat.txt`` into ``artifacts_dir`` and returns a
    parsed summary (changed files, insertions, deletions).
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo(str(worktree_path))

    if base_commit_sha:
        try:
            base_commit = repo.commit(base_commit_sha)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"base commit sha not resolvable: {base_commit_sha}"
            ) from exc
    else:
        try:
            base_commit = repo.commit(base_branch)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"base branch not resolvable: {base_branch}") from exc

    # Stage all worktree changes (modified + untracked) so the captured patch
    # exactly represents the agent's total delta against the base branch tip —
    # including new files the agent created but never `git add`ed / committed.
    # We stage into the index only; nothing is committed to the branch.
    repo.git.add("--all")

    # One diff of the (now fully-staged) index vs. base → complete patch.
    patch = repo.git.diff(base_commit, cached=True, no_color=True)
    stat = repo.git.diff(base_commit, cached=True, stat=True, no_color=True)
    changed_files, insertions, deletions = _parse_stat(stat)

    (artifacts_dir / "diff.patch").write_text(patch + "\n")
    (artifacts_dir / "diff_stat.txt").write_text(stat + "\n")

    return DiffCapture(
        patch=patch,
        stat=stat,
        changed_files=changed_files,
        insertions=insertions,
        deletions=deletions,
    )


def _parse_stat(stat_text: str) -> tuple[list[str], int, int]:
    """Extract changed file paths and total +/- from a ``git diff --stat`` block.

    Per-file lines look like `` README.md | 2 +-``. The trailing summary line
    looks like ``2 files changed, 12 insertions(+), 4 deletions(-)``.
    """
    changed: list[str] = []
    for line in stat_text.splitlines():
        # Per-file rows contain a '|' separator; the summary line doesn't.
        if "|" not in line:
            continue
        changed.append(line.split("|", 1)[0].strip())

    insertions = deletions = 0
    m = re.search(
        r"(\d+) files? changed(?:,\s*(\d+) insertions?\(\+\))?(?:,\s*(\d+) deletions?\(-\))?",
        stat_text,
    )
    if m:
        insertions = int(m.group(2) or 0)
        deletions = int(m.group(3) or 0)
    return changed, insertions, deletions
