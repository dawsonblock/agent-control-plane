"""Unit tests for the Obsidian vault frontmatter builder."""

from __future__ import annotations

from datetime import datetime, timezone

import yaml

from acp.gitops.diff import DiffCapture
from acp.models import (
    Recommendation,
    ReviewResult,
    RiskLevel,
    Task,
    TaskStatus,
)
from acp.vault.frontmatter import build_frontmatter


def _parse_frontmatter(fm: str) -> dict:
    """Strip Obsidian frontmatter delimiters before parsing."""
    stripped = fm.strip().removeprefix("---").removesuffix("---").strip()
    return yaml.safe_load(stripped)


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
        changed_files=["src/auth.py", "tests/test_auth.py"],
        concerns=[],
        summary="2 files changed, tests pass",
    )
    defaults.update(overrides)
    return ReviewResult(**defaults)


def _diff(**overrides) -> DiffCapture:
    defaults = dict(
        patch="@@ -1 +1 @@\n-old\n+new\n",
        stat="2 files changed, 5 insertions(+), 3 deletions(-)\n",
        changed_files=["src/auth.py", "tests/test_auth.py"],
        insertions=5,
        deletions=3,
    )
    defaults.update(overrides)
    return DiffCapture(**defaults)


def test_frontmatter_has_required_fields() -> None:
    fm = build_frontmatter(task=_task(), review=_review(), diff=_diff())
    parsed = _parse_frontmatter(fm)
    assert parsed["type"] == "task_report"
    assert parsed["task_id"] == "task_20260622_0001"
    assert parsed["repo"] == "demo"
    assert parsed["status"] == "created"
    assert parsed["approved"] is False
    assert parsed["memory_status"] == "draft"
    assert parsed["graphiti_ingested"] is False


def test_frontmatter_contains_review_data() -> None:
    fm = build_frontmatter(
        task=_task(),
        review=_review(risk=RiskLevel.HIGH, recommendation=Recommendation.REJECT),
        diff=_diff(),
    )
    parsed = _parse_frontmatter(fm)
    assert parsed["risk"] == "high"
    assert parsed["recommendation"] == "reject"


def test_frontmatter_contains_diff_stats() -> None:
    fm = build_frontmatter(task=_task(), review=_review(), diff=_diff(insertions=12, deletions=4))
    parsed = _parse_frontmatter(fm)
    assert parsed["files_changed"] == 2
    assert parsed["insertions"] == 12
    assert parsed["deletions"] == 4


def test_frontmatter_ships_unapproved() -> None:
    """Every vault note starts as unapproved draft — safety invariant."""
    fm = build_frontmatter(task=_task(), review=_review(), diff=_diff())
    parsed = _parse_frontmatter(fm)
    assert parsed["approved"] is False
    assert parsed["memory_status"] == "draft"
    assert parsed["graphiti_ingested"] is False


def test_frontmatter_has_sources_list() -> None:
    fm = build_frontmatter(task=_task(), review=_review(), diff=_diff())
    parsed = _parse_frontmatter(fm)
    assert "diff.patch" in parsed["sources"]
    assert "review.json" in parsed["sources"]


def test_frontmatter_accepts_explicit_date() -> None:
    dt = datetime(2026, 6, 22, tzinfo=timezone.utc)
    fm = build_frontmatter(task=_task(), review=_review(), diff=_diff(), today=dt)
    parsed = _parse_frontmatter(fm)
    assert parsed["created"] == "2026-06-22"


def test_frontmatter_is_valid_yaml_with_delimiters() -> None:
    fm = build_frontmatter(task=_task(), review=_review(), diff=_diff())
    assert fm.startswith("---\n")
    assert fm.endswith("---")
    # It should have exactly two `---` markers.
    assert fm.count("---") == 2