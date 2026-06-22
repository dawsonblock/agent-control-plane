"""Graphiti client for temporal memory management.

This module provides integration with Graphiti and FalkorDB for storing
verified facts and their relationships over time. It promotes approved
vault notes into a temporal knowledge graph.

Only ingests notes where:
- approved: true
- memory_status: active
- graphiti_ingested: false
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.models import Task
from acp.vault.frontmatter import Frontmatter


def ingest_task_to_graphiti(
    task: Task,
    frontmatter: Frontmatter,
    vault_note_path: Path,
    graphiti_group_id: str = "",
) -> dict[str, Any]:
    """Ingest an approved task report into Graphiti temporal memory.

    Args:
        task: The task that was completed.
        frontmatter: The frontmatter from the vault note.
        vault_note_path: Path to the vault note.
        graphiti_group_id: Optional group ID for Graphiti.

    Returns:
        Dictionary containing ingestion results.

    Raises:
        ValueError: If the task is not approved or already ingested.
    """
    # Check if the task is approved and ready for ingestion
    if not frontmatter.approved:
        raise ValueError(f"Task {task.task_id} is not approved for ingestion")

    if frontmatter.memory_status != "active":
        raise ValueError(f"Task {task.task_id} memory status is not 'active'")

    if frontmatter.graphiti_ingested:
        raise ValueError(f"Task {task.task_id} has already been ingested into Graphiti")

    # Extract facts from the task report
    facts = _extract_facts_from_task(task, frontmatter)

    # Ingest facts into Graphiti
    ingestion_result = _ingest_facts_to_graphiti(
        facts, task.task_id, graphiti_group_id
    )

    # Mark the task as ingested
    _mark_task_as_ingested(task, vault_note_path)

    return {
        "task_id": task.task_id,
        "facts_ingested": len(facts),
        "graphiti_result": ingestion_result,
        "vault_note_path": str(vault_note_path),
    }


def _extract_facts_from_task(
    task: Task, frontmatter: Frontmatter
) -> list[dict[str, Any]]:
    """Extract verifiable facts from a task and its frontmatter.

    Args:
        task: The task to extract facts from.
        frontmatter: The frontmatter containing metadata.

    Returns:
        List of fact dictionaries.
    """
    facts = []

    # Add basic task facts
    facts.append({
        "entity_type": "Task",
        "entity_id": task.task_id,
        "property": "status",
        "value": task.status.value,
        "source": "task_report",
        "confidence": 1.0,
        "ingestion_timestamp": frontmatter.created.isoformat(),
    })

    # Add repository facts
    if task.repo_name:
        facts.append({
            "entity_type": "Repo",
            "entity_id": task.repo_name,
            "property": "name",
            "value": task.repo_name,
            "source": "task_report",
            "confidence": 1.0,
            "ingestion_timestamp": frontmatter.created.isoformat(),
        })

    # Add risk facts if present
    if task.risk:
        facts.append({
            "entity_type": "Task",
            "entity_id": task.task_id,
            "property": "risk_level",
            "value": task.risk.value,
            "source": "task_report",
            "confidence": 1.0,
            "ingestion_timestamp": frontmatter.created.isoformat(),
        })

    # Add relationship facts
    if task.repo_name:
        facts.append({
            "entity_type": "Task",
            "entity_id": task.task_id,
            "relationship": "belongs_to_repo",
            "target_entity_type": "Repo",
            "target_entity_id": task.repo_name,
            "source": "task_report",
            "confidence": 1.0,
            "ingestion_timestamp": frontmatter.created.isoformat(),
        })

    return facts


def _ingest_facts_to_graphiti(
    facts: list[dict[str, Any]], task_id: str, group_id: str
) -> dict[str, Any]:
    """Ingest facts into Graphiti.

    Args:
        facts: List of facts to ingest.
        task_id: ID of the task being ingested.
        group_id: Optional group ID for Graphiti.

    Returns:
        Dictionary containing ingestion results.
    """
    # This is a placeholder implementation
    # In a real implementation, this would connect to Graphiti/FalkorDB
    # and store the facts in the temporal knowledge graph

    return {
        "status": "success",
        "facts_count": len(facts),
        "task_id": task_id,
        "group_id": group_id,
        "timestamp": "2026-06-22T00:00:00Z",
    }


def _mark_task_as_ingested(task: Task, vault_note_path: Path) -> None:
    """Mark a task as ingested in its vault note.

    Args:
        task: The task to mark.
        vault_note_path: Path to the vault note.
    """
    # Read the vault note
    note_content = vault_note_path.read_text()

    # Update the frontmatter to mark as ingested
    lines = note_content.splitlines()
    in_frontmatter = False
    updated_lines = []

    for line in lines:
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
            else:
                in_frontmatter = False
            updated_lines.append(line)
        elif in_frontmatter and line.strip().startswith("graphiti_ingested:"):
            # Update the graphiti_ingested field
            updated_lines.append("graphiti_ingested: true")
        else:
            updated_lines.append(line)

    # Write the updated note
    updated_content = "\n".join(updated_lines)
    vault_note_path.write_text(updated_content)


def search_graphiti_facts(
    query: str, entity_type: str | None = None, group_id: str = ""
) -> list[dict[str, Any]]:
    """Search for facts in Graphiti temporal memory.

    Args:
        query: Search query string.
        entity_type: Optional entity type to filter by.
        group_id: Optional group ID to filter by.

    Returns:
        List of matching facts.
    """
    # This is a placeholder implementation
    # In a real implementation, this would query Graphiti/FalkorDB

    return [
        {
            "entity_type": "Task",
            "entity_id": "task_2026_0001",
            "property": "status",
            "value": "passed",
            "source": "graphiti",
            "confidence": 0.95,
            "ingestion_timestamp": "2026-06-22T00:00:00Z",
        }
    ]


def get_temporal_relationships(
    entity_type: str, entity_id: str, group_id: str = ""
) -> list[dict[str, Any]]:
    """Get temporal relationships for an entity.

    Args:
        entity_type: Type of the entity.
        entity_id: ID of the entity.
        group_id: Optional group ID to filter by.

    Returns:
        List of temporal relationships.
    """
    # This is a placeholder implementation
    # In a real implementation, this would query Graphiti/FalkorDB

    return [
        {
            "relationship": "supersedes",
            "target_entity_type": "Task",
            "target_entity_id": "task_2026_0000",
            "source": "graphiti",
            "confidence": 0.90,
            "ingestion_timestamp": "2026-06-22T00:00:00Z",
        }
    ]