"""Obsidian writer — copies a report into the vault as a reviewable note.

The vault note is the *review surface*: frontmatter (lifecycle) + the report
body. It ships as ``approved: false`` and ``memory_status: draft``; a human
reads it, decides, and flips the flags. Only then may Graphiti (M7) ingest.

Critical safety property: this writer **never overwrites an already-approved
note**. If a note exists and was approved, re-running a task must not silently
clobber a human's decision. Uses the authoritative ``parse_frontmatter()``
parser — not a cheap line scan — so malformed frontmatter is also caught.

v0.5.14: The vault note is a **pure projection** of the event log + report.
It is never modified in-place. After lifecycle events (approve/reject), the
note is re-rendered from scratch using ``rerender_vault_note``, which reads
the current event log to determine the approval state and audit trail. This
eliminates the brittle 3-way transactional rollback that previously
synchronized in-place vault note edits with the event log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from acp.gitops.diff import DiffCapture
from acp.models import EventType, MemoryStatus, ReviewResult, Task, TaskStatus
from acp.vault.frontmatter import (
    Frontmatter,
    build_frontmatter,
    parse_frontmatter,
)


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

    Raises ``PermissionError`` if the destination already exists AND is
    approved (using the authoritative ``parse_frontmatter()`` parser).
    Malformed frontmatter also raises ``PermissionError`` — fail closed.
    Non-approved existing notes are overwritten (e.g. re-runs before
    approval).
    """
    vault_root = Path(vault_root)
    tasks_dir = vault_root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    # Guard against path traversal — task_id is used to construct the filename.
    # A task_id with "/" or ".." could escape the tasks directory.
    if "/" in task.task_id or "\\" in task.task_id or ".." in task.task_id:
        raise ValueError(f"task_id contains path separators — refusing to write: {task.task_id}")
    note_path = tasks_dir / f"{task.task_id}.md"

    if note_path.exists():
        existing = note_path.read_text()
        try:
            frontmatter, _ = parse_frontmatter(existing)
        except ValueError:
            raise PermissionError(
                f"refusing to overwrite note with malformed frontmatter: {note_path}"
            )
        if frontmatter.approved:
            raise PermissionError(
                f"refusing to overwrite an approved note: {note_path}"
            )

    frontmatter = build_frontmatter(task=task, review=review, diff=diff, today=today)
    note = f"{frontmatter}\n\n{report_body.lstrip()}\n"
    note_path.write_text(note)
    return note_path


def rerender_vault_note(
    *,
    note_path: Path,
    report_body: str,
    task: Task,
    review: ReviewResult,
    diff: DiffCapture,
    events: list,
    vault_root: Path,
    today: datetime | None = None,
) -> Path:
    """Re-render a vault note from scratch as a pure projection of state.

    This is the v0.5.14 pure-projection approach: instead of modifying the
    vault note in-place during approve/reject, we rebuild it entirely from
    the current state (task, review, diff, event log). The event log is the
    source of truth — the vault note is a derived view.

    The frontmatter is rebuilt with:
    - ``approved`` and ``memory_status`` derived from the event log
      (human.approved → approved=true, active; human.rejected → archived)
    - ``audit_trail`` built from lifecycle events in the log
    - ``status`` from the current task state

    The body is the current report (re-rendered with the updated event
    timeline).

    Unlike ``write_vault_note``, this function **does** overwrite approved
    notes — because it's re-rendering from the event log, which is the
    authority. The note is a projection, not a mutable artifact.
    """
    vault_root = Path(vault_root)
    if "/" in task.task_id or "\\" in task.task_id or ".." in task.task_id:
        raise ValueError(f"task_id contains path separators — refusing to write: {task.task_id}")

    # Derive approval state from the event log.
    approved = False
    memory_status = MemoryStatus.DRAFT.value
    audit_trail: list[dict[str, str]] = []

    for event in events:
        if event.type == EventType.HUMAN_APPROVED:
            approved = True
            memory_status = MemoryStatus.ACTIVE.value
            audit_trail.append({
                "action": "approved",
                "actor": event.payload.get("approver", "unknown"),
                "timestamp": event.timestamp,
            })
        elif event.type == EventType.HUMAN_REJECTED:
            memory_status = MemoryStatus.ARCHIVED.value
            audit_trail.append({
                "action": "rejected",
                "actor": event.payload.get("rejecter", "unknown"),
                "timestamp": event.timestamp,
            })

    # Build the frontmatter data directly (not via build_frontmatter, which
    # always sets approved=false). We need the event-log-derived values.
    created = (today or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    sources = ["diff.patch", "diff_stat.txt", "review.json", "commands.json"]
    data = {
        "type": "task_report",
        "task_id": task.task_id,
        "repo": task.repo_name,
        "branch": task.task_branch,
        "status": task.status.value,
        "risk": review.risk.value,
        "recommendation": review.recommendation.value,
        "approved": approved,
        "memory_status": memory_status,
        "graphiti_ingested": False,
        "created": created,
        "files_changed": len(diff.changed_files),
        "insertions": diff.insertions,
        "deletions": diff.deletions,
        "sources": sources,
    }
    if audit_trail:
        data["audit_trail"] = audit_trail

    yaml_body = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    frontmatter = f"---\n{yaml_body}\n---"

    note = f"{frontmatter}\n\n{report_body.lstrip()}\n"
    note_path.write_text(note)
    return note_path
