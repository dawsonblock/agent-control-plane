"""Memory promotion rules (M7).

Rules engine that determines whether an approved vault note should be
promoted to Graphiti temporal memory. The rules enforce safety properties:

  1. **Human firewall**: Only ``approved: true`` + ``memory_status: active``
     notes are eligible.
  2. **No failed tasks as successes**: ``TaskStatus.FAILED`` tasks can only
     be ingested as "known failures" (with a ``known_failure`` tag), never
     as "successful features."
  3. **High-risk gating**: If a task has ``RiskLevel.HIGH`` but was approved
     anyway, the system flags it for secondary review before promotion.
  4. **No re-ingestion**: Notes with ``graphiti_ingested: true`` are skipped.
  5. **ADR priority boost**: Architectural Decision Records (``type: decision``)
     get urgent priority — they constrain all future work and must be
     persisted first.
  6. **Reject recommendation exclusion**: If the automated review
     recommendation was ``reject``, the note is excluded even if a human
     approved it — the reviewer overrode the safety net, so we flag it.

The rules engine is pure — it takes data in and returns a decision. It does
not perform any side effects. The caller (CLI or auto_approve_node) is
responsible for the actual ingestion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.models import Task, TaskStatus
from acp.vault.frontmatter import Frontmatter

# --------------------------------------------------------------------------- #
# Promotion priority levels
# --------------------------------------------------------------------------- #

PRIORITY_LOW = 0  # No urgency — promote when convenient
PRIORITY_NORMAL = 1  # Standard approved task — promote on next cycle
PRIORITY_HIGH = 2  # High-risk approved task — flag for secondary review
PRIORITY_URGENT = 3  # Failed task with known failure — promote as cautionary

# Tasks that should NEVER be promoted.
PROMOTION_BLOCKED = -1


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def should_promote_to_graphiti(
    task: Task,
    frontmatter: dict[str, Any] | Frontmatter,
    vault_note_path: Path,
) -> bool:
    """Determine if a task should be promoted to Graphiti.

    Returns ``True`` only if ALL of the following are true:
      - The note is approved (``approved: true``)
      - The note is active (``memory_status: "active"``)
      - The note has not already been ingested (``graphiti_ingested: false``)
      - The vault note file exists on disk

    This is the **gate** — the caller should check this before calling
    :func:`ingest_task_to_graphiti`.

    Args:
        task: The task object.
        frontmatter: The parsed frontmatter (dict or Frontmatter).
        vault_note_path: Path to the vault note .md file.

    Returns:
        ``True`` if the task should be promoted, ``False`` otherwise.
    """
    fm = _normalize_frontmatter(frontmatter)

    # File must exist.
    if not vault_note_path.is_file():
        return False

    # Human firewall: must be approved + active.
    if not fm.approved:
        return False
    if fm.memory_status != "active":
        return False

    # No re-ingestion.
    if fm.graphiti_ingested:
        return False

    return True


def get_promotion_priority(
    task: Task,
    frontmatter: dict[str, Any] | Frontmatter,
    vault_note_path: Path,
) -> int:
    """Get the promotion priority for a task.

    Priority determines the order and urgency of promotion:
      - ``PROMOTION_BLOCKED`` (-1): Should not be promoted (check exclusions).
      - ``PRIORITY_LOW`` (0): No urgency.
      - ``PRIORITY_NORMAL`` (1): Standard approved task.
      - ``PRIORITY_HIGH`` (2): High-risk task — flag for secondary review.
      - ``PRIORITY_URGENT`` (3): Failed task — promote as known failure.

    Args:
        task: The task object.
        frontmatter: The parsed frontmatter.
        vault_note_path: Path to the vault note.

    Returns:
        Priority level (int). ``PROMOTION_BLOCKED`` if the task should
        not be promoted at all.
    """
    fm = _normalize_frontmatter(frontmatter)

    # If it shouldn't be promoted at all, return blocked.
    if not should_promote_to_graphiti(task, frontmatter, vault_note_path):
        return PROMOTION_BLOCKED

    # Failed tasks are promoted as "known failures" with urgent priority
    # so the system remembers what went wrong.
    if task.status == TaskStatus.FAILED:
        return PRIORITY_URGENT

    # Architectural Decision Records (ADRs) get top priority — these
    # are the most important facts to persist in the knowledge graph
    # because they constrain all future work on the repo.
    if fm.type == "decision":
        return PRIORITY_URGENT

    # High-risk tasks that were approved anyway get high priority
    # so they're flagged for secondary review before promotion.
    risk = (fm.risk or "").lower()
    if risk == "high":
        return PRIORITY_HIGH

    # Standard approved task.
    return PRIORITY_NORMAL


def get_promotion_exclusions(
    task: Task,
    frontmatter: dict[str, Any] | Frontmatter,
    vault_note_path: Path,
) -> list[str]:
    """Get reasons why a task should NOT be promoted to Graphiti.

    Returns a list of human-readable exclusion reasons. If the list is
    empty, the task is eligible for promotion (subject to the priority
    rules).

    Args:
        task: The task object.
        frontmatter: The parsed frontmatter.
        vault_note_path: Path to the vault note.

    Returns:
        List of exclusion reason strings. Empty if no exclusions.
    """
    fm = _normalize_frontmatter(frontmatter)
    exclusions: list[str] = []

    if not vault_note_path.is_file():
        exclusions.append(f"vault note not found: {vault_note_path}")
        return exclusions  # No point checking further

    if not fm.approved:
        exclusions.append("note is not approved (approved=false)")
    if fm.memory_status != "active":
        exclusions.append(f"memory_status is '{fm.memory_status}', not 'active'")
    if fm.graphiti_ingested:
        exclusions.append("already ingested into Graphiti (graphiti_ingested=true)")
    if (fm.recommendation or "").lower() == "reject":
        exclusions.append("automated review recommendation was 'reject'")

    return exclusions


def get_promotion_metadata(
    task: Task,
    frontmatter: dict[str, Any] | Frontmatter,
    vault_note_path: Path,
) -> dict[str, Any]:
    """Get metadata for promoting a task to Graphiti.

    Returns a dict with all the information needed for the ingestion:
      - ``eligible``: Whether the task should be promoted
      - ``priority``: Promotion priority level
      - ``exclusions``: List of exclusion reasons (empty if eligible)
      - ``task_id``: The task ID
      - ``repo_name``: The repo name
      - ``repo``: The repo name (alias for Graphiti node attributes)
      - ``branch_edited``: The task branch
      - ``risk``: The risk level
      - ``risk_level``: The risk level (alias for Graphiti node attributes)
      - ``is_known_failure``: Whether this is a failed task (promoted as cautionary)
      - ``needs_secondary_review``: Whether this is a high-risk task needing review
      - ``is_adr``: Whether this is an architectural decision record (type=decision)
      - ``files_changed``: Number of files changed (from frontmatter)
      - ``insertions``: Lines inserted (from frontmatter)
      - ``deletions``: Lines deleted (from frontmatter)
      - ``created_at``: Creation date (from frontmatter)
      - ``sources``: Evidence source files (from frontmatter)

    Args:
        task: The task object.
        frontmatter: The parsed frontmatter.
        vault_note_path: Path to the vault note.

    Returns:
        Metadata dict for the promotion decision.
    """
    fm = _normalize_frontmatter(frontmatter)
    eligible = should_promote_to_graphiti(task, frontmatter, vault_note_path)
    priority = get_promotion_priority(task, frontmatter, vault_note_path)
    exclusions = get_promotion_exclusions(task, frontmatter, vault_note_path)

    return {
        "eligible": eligible,
        "priority": priority,
        "exclusions": exclusions,
        "task_id": task.task_id,
        "repo_name": task.repo_name,
        "repo": task.repo_name,
        "branch_edited": task.task_branch,
        "risk": fm.risk,
        "risk_level": fm.risk or "unknown",
        "is_known_failure": task.status == TaskStatus.FAILED,
        "needs_secondary_review": (fm.risk or "").lower() == "high",
        "is_adr": fm.type == "decision",
        "files_changed": fm.files_changed or 0,
        "insertions": fm.insertions or 0,
        "deletions": fm.deletions or 0,
        "created_at": fm.created or "",
        "sources": fm.sources,
    }


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _normalize_frontmatter(
    fm: dict[str, Any] | Frontmatter,
) -> Frontmatter:
    """Accept either a dict or Frontmatter and return a Frontmatter."""
    if isinstance(fm, dict):
        return Frontmatter(**fm)
    return fm
