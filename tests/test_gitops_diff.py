"""Tests for acp.gitops.diff — diff capture, stat parsing, ignore patterns."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acp.gitops.diff import (
    DiffCapture,
    _matches_ignore_pattern,
    _parse_stat,
    capture_diff,
)
from acp.gitops.worktrees import create_worktree


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ---------------------------------------------------------------------------
# _matches_ignore_pattern
# ---------------------------------------------------------------------------


def test_matches_ignore_pattern_pyc():
    assert _matches_ignore_pattern("foo.pyc")
    assert _matches_ignore_pattern("a/b/foo.pyc")


def test_matches_ignore_pattern_pycache():
    assert _matches_ignore_pattern("__pycache__/foo.pyc")
    assert _matches_ignore_pattern("__pycache__/bar.py")


def test_matches_ignore_pattern_normal_file():
    assert not _matches_ignore_pattern("src/acp/diff.py")
    assert not _matches_ignore_pattern("README.md")


def test_matches_ignore_pattern_nested():
    assert _matches_ignore_pattern("src/__pycache__/foo.pyc")
    assert _matches_ignore_pattern("tests/__pycache__/sub/bar.pyc")


# ---------------------------------------------------------------------------
# _parse_stat
# ---------------------------------------------------------------------------


def test_parse_stat_single_file():
    stat = " README.md | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
    changed, ins, dele = _parse_stat(stat)
    assert changed == ["README.md"]
    assert ins == 1
    assert dele == 1


def test_parse_stat_multiple_files():
    stat = (
        " README.md  | 2 +-\n"
        " src/foo.py | 10 +++++++---\n"
        " 2 files changed, 8 insertions(+), 4 deletions(-)\n"
    )
    changed, ins, dele = _parse_stat(stat)
    assert changed == ["README.md", "src/foo.py"]
    assert ins == 8
    assert dele == 4


def test_parse_stat_insertions_only():
    stat = " README.md | 3 +++\n 1 file changed, 3 insertions(+)\n"
    changed, ins, dele = _parse_stat(stat)
    assert changed == ["README.md"]
    assert ins == 3
    assert dele == 0


def test_parse_stat_deletions_only():
    stat = " README.md | 2 --\n 1 file changed, 2 deletions(-)\n"
    changed, ins, dele = _parse_stat(stat)
    assert changed == ["README.md"]
    assert ins == 0
    assert dele == 2


def test_parse_stat_empty():
    changed, ins, dele = _parse_stat("")
    assert changed == []
    assert ins == 0
    assert dele == 0


def test_parse_stat_no_changes():
    stat = "0 files changed\n"
    changed, ins, dele = _parse_stat(stat)
    assert changed == []
    assert ins == 0
    assert dele == 0


# ---------------------------------------------------------------------------
# DiffCapture dataclass
# ---------------------------------------------------------------------------


def test_diff_capture_dataclass():
    cap = DiffCapture(
        patch="",
        stat="",
        changed_files=[],
        insertions=0,
        deletions=0,
    )
    assert cap.binary_files == []


# ---------------------------------------------------------------------------
# capture_diff (integration)
# ---------------------------------------------------------------------------


def test_capture_diff_real_repo(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    worktree_dir = tmp_path / "wt"
    artifacts_dir = tmp_path / "artifacts"

    worktree_path, base_sha = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/diff-real",
        target_path=worktree_dir,
    )

    # Make a change inside the worktree.
    (worktree_path / "new_file.py").write_text("print('hello')\n")
    (worktree_path / "README.md").write_text("# changed\n")

    diff = capture_diff(
        worktree_path=worktree_path,
        base_branch="main",
        artifacts_dir=artifacts_dir,
    )

    # Artifacts written.
    assert (artifacts_dir / "diff.patch").is_file()
    assert (artifacts_dir / "diff_stat.txt").is_file()
    assert (artifacts_dir / "diff.patch").read_text().strip()

    # new_file.py is fully added; README.md changed.
    assert "new_file.py" in diff.changed_files
    assert "README.md" in diff.changed_files
    assert diff.insertions >= 1
    assert diff.deletions >= 1


def test_capture_diff_ignores_pycache(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    worktree_dir = tmp_path / "wt"
    artifacts_dir = tmp_path / "artifacts"

    worktree_path, _ = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/diff-pycache",
        target_path=worktree_dir,
    )

    # Real change + junk.
    (worktree_path / "real.py").write_text("x = 1\n")
    pycache = worktree_path / "__pycache__"
    pycache.mkdir()
    (pycache / "junk.pyc").write_text("binary junk")
    (worktree_path / "module.pyc").write_text("more junk")

    diff = capture_diff(
        worktree_path=worktree_path,
        base_branch="main",
        artifacts_dir=artifacts_dir,
    )

    assert "real.py" in diff.changed_files
    assert not any(f.endswith(".pyc") for f in diff.changed_files)
    assert not any("__pycache__" in f for f in diff.changed_files)


def test_capture_diff_base_commit_sha(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    worktree_dir = tmp_path / "wt"
    artifacts_dir = tmp_path / "artifacts"

    worktree_path, base_sha = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/diff-sha",
        target_path=worktree_dir,
    )

    (worktree_path / "new.py").write_text("y = 2\n")

    diff = capture_diff(
        worktree_path=worktree_path,
        base_branch="main",
        artifacts_dir=artifacts_dir,
        base_commit_sha=base_sha,
    )

    assert "new.py" in diff.changed_files
    assert diff.insertions >= 1


def test_capture_diff_invalid_base_branch(disposable_repo, tmp_path):
    repo_path = disposable_repo.path
    worktree_dir = tmp_path / "wt"
    artifacts_dir = tmp_path / "artifacts"

    worktree_path, _ = create_worktree(
        repo_path=repo_path,
        base_branch="main",
        branch_name="agent/diff-invalid",
        target_path=worktree_dir,
    )
    (worktree_path / "x.py").write_text("x = 1\n")

    with pytest.raises(ValueError):
        capture_diff(
            worktree_path=worktree_path,
            base_branch="does-not-exist",
            artifacts_dir=artifacts_dir,
        )
