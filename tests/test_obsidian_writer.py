"""Tests for the Obsidian vault writer — including overwrite protection."""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.gitops.diff import DiffCapture
from acp.models import Recommendation, ReviewResult, RiskLevel, Task
from acp.vault.obsidian_writer import write_vault_note


def _task(**overrides) -> Task:
    defaults = dict(
        task_id="task_20260622_0001",
        repo_name="demo",
        repo_path="/tmp/repo",
        base_branch="main",
        task_branch="agent/task_20260622_0001",
        worktree_path="/tmp/worktree",
        user_request="fix the auth test",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _review(**overrides) -> ReviewResult:
    defaults = dict(
        risk=RiskLevel.LOW,
        recommendation=Recommendation.MERGE,
        changed_files=["src/auth.py"],
        concerns=[],
        summary="ok",
    )
    defaults.update(overrides)
    return ReviewResult(**defaults)


def _diff(**overrides) -> DiffCapture:
    defaults = dict(
        patch="@@ -1 +1 @@\n-old\n+new\n",
        stat="1 file changed, 1 insertion(+), 1 deletion(-)\n",
        changed_files=["src/auth.py"],
        insertions=1,
        deletions=1,
    )
    defaults.update(overrides)
    return DiffCapture(**defaults)


def test_write_new_vault_note(tmp_path: Path) -> None:
    """A fresh note is written without error."""
    note_path = write_vault_note(
        report_body="# Report body",
        task=_task(),
        review=_review(),
        diff=_diff(),
        vault_root=tmp_path,
    )
    assert note_path.exists()
    body = note_path.read_text()
    assert "approved: false" in body
    assert "memory_status: draft" in body


def test_overwrite_unapproved_note_allowed(tmp_path: Path) -> None:
    """An unapproved existing note can be overwritten (re-run before review)."""
    t = _task(task_id="task_overwrite_test")
    write_vault_note(
        report_body="# First run",
        task=t,
        review=_review(),
        diff=_diff(),
        vault_root=tmp_path,
    )
    # Second write with same task id — should succeed (not approved).
    write_vault_note(
        report_body="# Second run",
        task=t,
        review=_review(),
        diff=_diff(),
        vault_root=tmp_path,
    )
    body = (tmp_path / "tasks" / "task_overwrite_test.md").read_text()
    assert "Second run" in body


def test_overwrite_approved_note_raises(tmp_path: Path) -> None:
    """An approved existing note RAISES — human decision must not be clobbered."""
    t = _task(task_id="task_approved_test")
    # Write first note, then manually flip approved: true in the file.
    write_vault_note(
        report_body="# First run",
        task=t,
        review=_review(),
        diff=_diff(),
        vault_root=tmp_path,
    )
    note_path = tmp_path / "tasks" / "task_approved_test.md"
    content = note_path.read_text()
    content = content.replace("approved: false", "approved: true")
    note_path.write_text(content)

    # Second write should raise PermissionError.
    with pytest.raises(PermissionError, match="refusing to overwrite an approved note"):
        write_vault_note(
            report_body="# Second run",
            task=t,
            review=_review(),
            diff=_diff(),
            vault_root=tmp_path,
        )

    # Original content preserved.
    final = note_path.read_text()
    assert "First run" in final
    assert "Second run" not in final


def test_overwrite_malformed_frontmatter_raises(tmp_path: Path) -> None:
    """A note with malformed frontmatter RAISES — fail closed, never overwrite."""
    t = _task(task_id="task_malformed_test")
    note_path = tmp_path / "tasks" / "task_malformed_test.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a note with broken frontmatter (missing closing ---).
    note_path.write_text("---\napproved: true\n\n# body\n")

    with pytest.raises(PermissionError, match="malformed frontmatter"):
        write_vault_note(
            report_body="# Should not write",
            task=t,
            review=_review(),
            diff=_diff(),
            vault_root=tmp_path,
        )


def test_overwrite_approved_via_frontmatter_parser(tmp_path: Path) -> None:
    """Approved detection uses parse_frontmatter(), not line scan.

    The word 'approved:' in body text should NOT trigger the check.
    Only the frontmatter value matters.
    """
    t = _task(task_id="task_parser_test")
    note_path = write_vault_note(
        report_body="# First run\nThis note says approved: false in body text.",
        task=t,
        review=_review(),
        diff=_diff(),
        vault_root=tmp_path,
    )
    # Flip approval properly via frontmatter.
    content = note_path.read_text()
    content = content.replace("approved: false", "approved: true")
    note_path.write_text(content)

    # Second write should raise because frontmatter says approved.
    with pytest.raises(PermissionError, match="refusing to overwrite an approved note"):
        write_vault_note(
            report_body="# Second run",
            task=t,
            review=_review(),
            diff=_diff(),
            vault_root=tmp_path,
        )

    # Original content preserved.
    final = note_path.read_text()
    assert "First run" in final
