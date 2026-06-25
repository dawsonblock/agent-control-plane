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

# Default ignore patterns for generated/junk files that should never appear
# in a captured diff. These are applied in addition to the repo's own
# .gitignore. Without this, running validation commands (pytest, mypy, etc.)
# inside the worktree can create __pycache__/, *.pyc, .pytest_cache/ etc.
# that get staged by `git add --all` and pollute the evidence diff.
DEFAULT_IGNORE_PATTERNS = [
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".DS_Store",
]


@dataclass
class DiffCapture:
    patch: str
    stat: str
    changed_files: list[str]
    insertions: int
    deletions: int
    binary_files: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.binary_files is None:
            self.binary_files = []


def _matches_ignore_pattern(path_str: str) -> bool:
    """Check if a path matches any of the default ignore patterns."""
    parts = path_str.replace("\\", "/").split("/")
    for pattern in DEFAULT_IGNORE_PATTERNS:
        pat = pattern.rstrip("/")
        if pat.startswith("*"):
            # Suffix match (e.g. *.pyc)
            suffix = pat[1:]
            if any(p.endswith(suffix) for p in parts):
                return True
        else:
            if pat in parts:
                return True
    return False


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

    Generated junk (``__pycache__``, ``*.pyc``, ``.pytest_cache``, etc.) is
    filtered out so the evidence diff contains only meaningful changes.
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

    # Unstage generated junk files that were caught by `git add --all` but
    # shouldn't be in the evidence diff. We check what's staged and remove
    # anything matching our default ignore patterns.
    try:
        staged_files = repo.git.diff("--cached", "--name-only").splitlines()
        junk_to_unstage = [f for f in staged_files if _matches_ignore_pattern(f)]
        if junk_to_unstage:
            repo.git.reset("--", *junk_to_unstage)
    except Exception:  # noqa: BLE001
        pass  # best-effort; if reset fails, the diff just includes the junk

    # One diff of the (now fully-staged) index vs. base → complete patch.
    patch = repo.git.diff(base_commit, cached=True, no_color=True)
    stat = repo.git.diff(base_commit, cached=True, stat=True, no_color=True)
    changed_files, insertions, deletions = _parse_stat(stat)

    # Detect binary files in the diff — git marks them as
    # "Binary files a/path and b/path differ" or
    # "Binary files /dev/null and b/path differ"
    binary_files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("Binary files ") and " differ" in line:
            # Extract the file path from "Binary files <old> and <new> differ"
            parts = line.split()
            if len(parts) >= 5:
                # Use the "b/" version (the new file) — parts[4]
                b_path = parts[4]
                if b_path.startswith("b/"):
                    b_path = b_path[2:]
                binary_files.append(b_path)

    (artifacts_dir / "diff.patch").write_text(patch + "\n")
    (artifacts_dir / "diff_stat.txt").write_text(stat + "\n")

    return DiffCapture(
        patch=patch,
        stat=stat,
        changed_files=changed_files,
        insertions=insertions,
        deletions=deletions,
        binary_files=binary_files,
    )


def capture_diff_from_remote(
    repo_path: Path,
    remote: str,
    base_branch: str,
    artifacts_dir: Path,
    remote_branch: str = "main",
) -> DiffCapture:
    """Diff the sandbox remote's branch against ``base_branch``.

    Used by the ``docker_sbx`` executor backend: after the agent finishes
    inside the sandbox, ACP fetches the sandbox remote and captures the diff
    between the sandbox's branch and the repo's base branch. The diff comes
    from the sandbox's private clone, not from host worktree mutation.

    Writes ``diff.patch`` and ``diff_stat.txt`` into ``artifacts_dir``.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo(str(repo_path))

    remote_ref = f"{remote}/{remote_branch}"

    # Resolve the base commit.
    try:
        base_commit = repo.commit(base_branch)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"base branch not resolvable: {base_branch}") from exc

    # Resolve the remote ref (the sandbox's branch tip).
    try:
        remote_commit = repo.commit(remote_ref)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"sandbox remote ref not resolvable: {remote_ref}. "
            f"Did you fetch the remote first?"
        ) from exc

    # Diff base..remote — the complete delta the agent produced.
    # Use hexsha strings to ensure git diff works with any ref type.
    base_sha = base_commit.hexsha
    remote_sha = remote_commit.hexsha
    patch = repo.git.diff(base_sha, remote_sha, no_color=True)
    stat = repo.git.diff(base_sha, remote_sha, stat=True, no_color=True)
    changed_files, insertions, deletions = _parse_stat(stat)

    # Detect binary files.
    binary_files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("Binary files ") and " differ" in line:
            parts = line.split()
            if len(parts) >= 5:
                b_path = parts[4]
                if b_path.startswith("b/"):
                    b_path = b_path[2:]
                binary_files.append(b_path)

    (artifacts_dir / "diff.patch").write_text(patch + "\n")
    (artifacts_dir / "diff_stat.txt").write_text(stat + "\n")

    return DiffCapture(
        patch=patch,
        stat=stat,
        changed_files=changed_files,
        insertions=insertions,
        deletions=deletions,
        binary_files=binary_files,
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
