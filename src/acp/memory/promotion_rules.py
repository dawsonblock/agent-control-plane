"""Memory promotion rules — deferred until M7.

M7 will implement the logic for promoting approved vault notes into Graphiti
temporal memory. Until then, ``should_promote_to_graphiti`` always returns
``False`` and ``get_promotion_priority`` raises ``NotImplementedError``.

This prevents accidental memory ingestion before the infrastructure exists.
See docs/roadmap.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.models import Task


def should_promote_to_graphiti(
    task: Task, frontmatter: dict[str, Any], vault_note_path: Path
) -> bool:
    """Determine if a task should be promoted to Graphiti.

    Always returns ``False`` until M7 — the promotion infrastructure does
    not exist yet.
    """
    return False


def get_promotion_priority(
    task: Task, frontmatter: dict[str, Any], vault_note_path: Path
) -> int:
    """Get the promotion priority for a task.

    Raises:
        NotImplementedError: Always — M7 deferred. See docs/roadmap.md.
    """
    raise NotImplementedError(
        "Promotion priority is not implemented until M7. "
        "See docs/roadmap.md."
    )


def get_promotion_exclusions(
    task: Task, frontmatter: dict[str, Any], vault_note_path: Path
) -> list[str]:
    """Get reasons why a task should NOT be promoted to Graphiti.

    Args:
        task: The task to evaluate.
        frontmatter: The frontmatter from the vault note.
        vault_note_path: Path to the vault note.

    Returns:
        List of exclusion reasons.
    Raises:
        NotImplementedError: Always — M7 deferred. See docs/roadmap.md.
    """
    raise NotImplementedError(
        "Promotion exclusions are not implemented until M7. "
        "See docs/roadmap.md."
    )


def get_promotion_metadata(
    task: Task, frontmatter: dict[str, Any], vault_note_path: Path
) -> dict[str, Any]:
    """Get metadata for promoting a task to Graphiti.

    Raises:
        NotImplementedError: Always — M7 deferred. See docs/roadmap.md.
    """
    raise NotImplementedError(
        "Promotion metadata is not implemented until M7. "
        "See docs/roadmap.md."
    )