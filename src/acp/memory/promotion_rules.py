"""Memory promotion rules for Graphiti integration.

This module defines the rules for promoting approved vault notes into
Graphiti temporal memory. It implements the logic for determining which
notes should be promoted and when.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.models import Task, TaskStatus
from acp.vault.frontmatter import Frontmatter


def should_promote_to_graphiti(
    task: Task, frontmatter: Frontmatter, vault_note_path: Path
) -> bool:
    """Determine if a task should be promoted to Graphiti.

    Args:
        task: The task to evaluate.
        frontmatter: The frontmatter from the vault note.
        vault_note_path: Path to the vault note.

    Returns:
        True if the task should be promoted to Graphiti, False otherwise.
    """
    # Check if the task is approved
    if not frontmatter.approved:
        return False

    # Check if the task is active
    if frontmatter.memory_status != "active":
        return False

    # Check if the task has already been ingested
    if frontmatter.graphiti_ingested:
        return False

    # Check if the task has a valid status
    if task.status not in (TaskStatus.PASSED, TaskStatus.FAILED):
        return False

    # Check if the task has a risk level
    if not task.risk:
        return False

    # Check if the task has a repository
    if not task.repo_name:
        return False

    # All checks passed
    return True


def get_promotion_priority(
    task: Task, frontmatter: Frontmatter, vault_note_path: Path
) -> int:
    """Get the promotion priority for a task.

    Args:
        task: The task to evaluate.
        frontmatter: The frontmatter from the vault note.
        vault_note_path: Path to the vault note.

    Returns:
        Priority level (higher number = higher priority).
    """
    priority = 0

    # Higher priority for passed tasks
    if task.status == TaskStatus.PASSED:
        priority += 10

    # Higher priority for tasks with low risk
    if task.risk and task.risk.value == "low":
        priority += 5

    # Higher priority for tasks with recent timestamps
    if frontmatter.created:
        days_since_creation = (frontmatter.created.date() - Path(
            "today"
        ).stat().st_mtime).days
        if days_since_creation <= 1:
            priority += 3

    # Higher priority for tasks with existing dependencies
    if task.dependencies:
        priority += 2

    return priority


def get_promotion_exclusions(
    task: Task, frontmatter: Frontmatter, vault_note_path: Path
) -> list[str]:
    """Get reasons why a task should NOT be promoted to Graphiti.

    Args:
        task: The task to evaluate.
        frontmatter: The frontmatter from the vault note.
        vault_note_path: Path to the vault note.

    Returns:
        List of exclusion reasons.
    """
    exclusions = []

    # Check if the task is approved
    if not frontmatter.approved:
        exclusions.append("not_approved")

    # Check if the task is active
    if frontmatter.memory_status != "active":
        exclusions.append("not_active")

    # Check if the task has already been ingested
    if frontmatter.graphiti_ingested:
        exclusions.append("already_ingested")

    # Check if the task has a valid status
    if task.status not in (TaskStatus.PASSED, TaskStatus.FAILED):
        exclusions.append("invalid_status")

    # Check if the task has a risk level
    if not task.risk:
        exclusions.append("no_risk_level")

    # Check if the task has a repository
    if not task.repo_name:
        exclusions.append("no_repo")

    # Check if the task has a valid risk level
    if task.risk and task.risk.value not in ("low", "medium", "high"):
        exclusions.append("invalid_risk_level")

    return exclusions


def get_promotion_metadata(
    task: Task, frontmatter: Frontmatter, vault_note_path: Path
) -> dict[str, Any]:
    """Get metadata for promoting a task to Graphiti.

    Args:
        task: The task to promote.
        frontmatter: The frontmatter from the vault note.
        vault_note_path: Path to the vault note.

    Returns:
        Dictionary containing promotion metadata.
    """
    return {
        "task_id": task.task_id,
        "repo_name": task.repo_name,
        "status": task.status.value,
        "risk_level": task.risk.value if task.risk else None,
        "created": frontmatter.created.isoformat(),
        "approved": frontmatter.approved,
        "memory_status": frontmatter.memory_status,
        "graphiti_ingested": frontmatter.graphiti_ingested,
        "priority": get_promotion_priority(task, frontmatter, vault_note_path),
        "exclusions": get_promotion_exclusions(task, frontmatter, vault_note_path),
        "source": "vault_note",
        "vault_note_path": str(vault_note_path),
    }