"""Graphiti client — deferred until M7.

M7 will implement real integration with Graphiti + FalkorDB for temporal
memory. Until then, every function raises ``NotImplementedError`` so it's
impossible to accidentally promote memory without the infrastructure in
place. See docs/roadmap.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.models import Task


def ingest_task_to_graphiti(
    task: Task,
    frontmatter: dict[str, Any],
    vault_note_path: Path,
    graphiti_group_id: str = "",
) -> dict[str, Any]:
    """Ingest an approved task report into Graphiti temporal memory.

    Raises:
        NotImplementedError: Always — M7 deferred. See docs/roadmap.md.
    """
    raise NotImplementedError(
        "Graphiti ingestion is not implemented until M7. "
        "See docs/roadmap.md."
    )


def search_graphiti_facts(
    query: str, entity_type: str | None = None, group_id: str = ""
) -> list[dict[str, Any]]:
    """Search for facts in Graphiti temporal memory.

    Raises:
        NotImplementedError: Always — M7 deferred. See docs/roadmap.md.
    """
    raise NotImplementedError(
        "Graphiti search is not implemented until M7. "
        "See docs/roadmap.md."
    )


def get_temporal_relationships(
    entity_type: str, entity_id: str, group_id: str = ""
) -> list[dict[str, Any]]:
    """Get temporal relationships for an entity.

    Raises:
        NotImplementedError: Always — M7 deferred. See docs/roadmap.md.
    """
    raise NotImplementedError(
        "Graphiti temporal relationships are not implemented until M7. "
        "See docs/roadmap.md."
    )