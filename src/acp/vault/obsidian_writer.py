"""Obsidian writer — copies a report into the vault as a reviewable note.

The vault note is the *review surface*: frontmatter (lifecycle) + the report
body. It ships as ``approved: false`` and ``memory_status: draft``; a human
reads it, decides, and flips the flags. Only then may Graphiti (M7) ingest.

Critical safety property: this writer **never overwrites an already-approved
note**. If a note exists and was approved, re-running a task must not silently
clobber a human's decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from acp.gitops.diff import DiffCapture
from acp.models import ReviewResult, Task
from acp.vault.frontmatter import build_frontmatter


def _looks_approved(existing_text: str) -> bool:
    """Cheap, dependency-free check for `approved: true` in frontmatter."""
    for line in existing_text.splitlines():
        s = line.strip()
        if s == "---":
            continue
        if s.startswith("approved:"):
            return "true" in s.lower()
    return False


def write_vault_note(
    *,
    report_body: str,
    task: Task,
    review: ReviewResult,
    diff: DiffCapture,
    vault_root: Path,
    today: datetime | None = None,
) -> Path:
    """Write ``vault/tasks/<task_id>.md`` (frontmatter + report body).

    Raises if the destination already exists AND is approved — protecting the
    human's prior decision. Non-approved existing notes are overwritten (e.g.
    re-runs before approval).
    """
    vault_root = Path(vault_root)
    tasks_dir = vault_root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    note_path = tasks_dir / f"{task.task_id}.md"

    if note_path.exists() and _looks_approved(note_path.read_text()):
        raise PermissionError(
            f"refusing to overwrite an approved note: {note_path}"
        )

    frontmatter = build_frontmatter(task=task, review=review, diff=diff, today=today)
    note = f"{frontmatter}\n\n{report_body.lstrip()}\n"
    note_path.write_text(note)
    return note_path
