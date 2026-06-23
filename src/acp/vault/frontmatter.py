"""Frontmatter builder for Obsidian task notes.

Produces the YAML frontmatter block that makes a task report machine-readable
*and* human-visible inside Obsidian. The frontmatter is the lifecycle record:
``approved`` and ``memory_status`` are what the human (and later Graphiti
ingestion) keys off.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yaml

from pydantic import BaseModel

from acp.gitops.diff import DiffCapture
from acp.models import MemoryStatus, ReviewResult, Task


def build_frontmatter(
    *,
    task: Task,
    review: ReviewResult,
    diff: DiffCapture,
    today: datetime | None = None,
) -> str:
    """Return a YAML frontmatter block (with leading and trailing ``---``)."""
    created = (today or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    sources = [
        "diff.patch",
        "diff_stat.txt",
        "review.json",
        "commands.json",
    ]
    data = {
        "type": "task_report",
        "task_id": task.task_id,
        "repo": task.repo_name,
        "branch": task.task_branch,
        "status": task.status.value,
        "risk": review.risk.value,
        "recommendation": review.recommendation.value,
        "approved": False,                  # human must flip this
        "memory_status": MemoryStatus.DRAFT.value,
        "graphiti_ingested": False,
        "created": created,
        "files_changed": len(diff.changed_files),
        "insertions": diff.insertions,
        "deletions": diff.deletions,
        "sources": sources,
    }
    # Pydantic models round-trip fine through yaml.safe_dump; keep block style
    # and deterministic key order for clean diffs in git.
    body = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{body}\n---"


class Frontmatter(BaseModel):
    """Parsed frontmatter from an Obsidian task note."""

    type: str
    task_id: str | None = None
    repo: str | None = None
    branch: str | None = None
    status: str | None = None
    risk: str | None = None
    recommendation: str | None = None
    approved: bool = False
    memory_status: str = "draft"
    graphiti_ingested: bool = False
    created: str | None = None
    files_changed: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    sources: list[str] = []


def parse_frontmatter(markdown: str) -> tuple[Frontmatter, str]:
    """Parse YAML frontmatter from a Markdown note.

    Returns ``(Frontmatter, body)`` where ``body`` is everything after the
    closing ``---``.  Raises ``ValueError`` if frontmatter is missing or
    unparseable.
    """
    stripped = markdown.lstrip()
    if not stripped.startswith("---"):
        raise ValueError("Missing frontmatter: expected leading '---'")

    # Split on the second '---'
    parts = stripped[3:].split("---", 1)
    if len(parts) < 2:
        raise ValueError("Missing closing '---' in frontmatter")

    yaml_text = parts[0].strip()
    body = parts[1].strip()

    raw = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    return Frontmatter(**raw), body
