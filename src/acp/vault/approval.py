"""Vault note approval — the human decision surface.

This is the single most important safety property in ACP: **the system
cannot gaslight itself**, because every fact it remembers was first read
and approved by a human. ``approve_vault_note`` flips the frontmatter
flags that gate memory promotion; ``reject_vault_note`` archives a note
so it can never be promoted.

Both functions:
  - Parse the existing frontmatter (using the authoritative parser)
  - Refuse to operate on already-approved/already-rejected notes
  - Re-write the note with updated frontmatter + an audit trail comment
  - Return the updated frontmatter for the caller to record as an event

The caller (CLI) is responsible for writing the ``human.approved`` or
``human.rejected`` event to the event log and updating the task status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from acp.models import MemoryStatus, TaskStatus
from acp.vault.frontmatter import Frontmatter, parse_frontmatter


def approve_vault_note(
    note_path: Path,
    *,
    approver: str = "",
    now: datetime | None = None,
) -> Frontmatter:
    """Flip a vault note's ``approved`` to ``true`` and ``memory_status`` to ``active``.

    Raises:
        FileNotFoundError: if the note doesn't exist.
        PermissionError: if the note is already approved.
        ValueError: if the frontmatter is malformed.
    """
    return _update_note(
        note_path,
        approved=True,
        memory_status=MemoryStatus.ACTIVE.value,
        action="approved",
        actor=approver,
        now=now,
    )


def reject_vault_note(
    note_path: Path,
    *,
    rejecter: str = "",
    now: datetime | None = None,
) -> Frontmatter:
    """Archive a vault note so it can never be promoted to memory.

    Sets ``memory_status`` to ``archived``. Does NOT set ``approved`` —
    a rejected note is explicitly not approved. Raises if already approved
    (can't reject after approval) or already archived.

    Raises:
        FileNotFoundError: if the note doesn't exist.
        PermissionError: if the note is already approved.
        ValueError: if the frontmatter is malformed.
    """
    return _update_note(
        note_path,
        approved=False,
        memory_status=MemoryStatus.ARCHIVED.value,
        action="rejected",
        actor=rejecter,
        now=now,
    )


def _update_note(
    note_path: Path,
    *,
    approved: bool,
    memory_status: str,
    action: str,
    actor: str,
    now: datetime | None,
) -> Frontmatter:
    """Internal: parse, update, and re-write a vault note's frontmatter."""
    if not note_path.is_file():
        raise FileNotFoundError(f"vault note not found: {note_path}")

    content = note_path.read_text()
    frontmatter, body = parse_frontmatter(content)

    # Safety checks — fail closed.
    if action == "approved" and frontmatter.approved:
        raise PermissionError(
            f"note is already approved — cannot approve again: {note_path}"
        )
    if action == "rejected" and frontmatter.approved:
        raise PermissionError(
            f"note is already approved — cannot reject after approval: {note_path}"
        )
    if action == "rejected" and frontmatter.memory_status == MemoryStatus.ARCHIVED.value:
        raise PermissionError(
            f"note is already archived — cannot reject again: {note_path}"
        )

    # Update the frontmatter fields.
    timestamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    frontmatter.approved = approved
    frontmatter.memory_status = memory_status

    # Append to the audit trail (preserves existing entries from the model).
    frontmatter.audit_trail.append({
        "action": action,
        "actor": actor or "unknown",
        "timestamp": timestamp,
    })

    # Re-serialize frontmatter to YAML.
    data = frontmatter.model_dump()

    yaml_body = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()

    new_content = f"---\n{yaml_body}\n---\n\n{body}\n"
    note_path.write_text(new_content)

    return frontmatter


def can_approve(task_status: TaskStatus) -> bool:
    """Whether a task in the given status is eligible for approval.

    Only ``PASSED`` and ``NEEDS_REVIEW`` tasks can be approved. ``FAILED``
    tasks cannot (they didn't pass the gates). ``APPROVED`` tasks are
    already approved. ``ARCHIVED`` tasks are rejected.
    """
    return task_status in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW)
