"""Frontmatter builder for Obsidian task notes.

Produces the YAML frontmatter block that makes a task report machine-readable
*and* human-visible inside Obsidian. The frontmatter is the lifecycle record:
``approved`` and ``memory_status`` are what the human (and later Graphiti
ingestion) keys off.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yaml

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
