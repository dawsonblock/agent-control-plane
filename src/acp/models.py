"""Typed data models — the shared vocabulary of the control plane.

Everything that crosses a layer boundary is one of these types. They match
the JSON shapes in docs/architecture.md and the spec's data-model section.
Events and reports are truth; these models enforce that truth is well-formed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with a trailing Z, e.g. 2026-06-21T12:00:00Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class TaskStatus(str, Enum):
    """Lifecycle of a task. Every transition is an event."""

    CREATED = "created"
    WORKTREE_CREATED = "worktree_created"
    CONTEXT_BUILT = "context_built"
    PLANNED = "planned"
    EXECUTING = "executing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    REPORT_WRITTEN = "report_written"
    PASSED = "passed"
    FAILED = "failed"
    APPROVED = "approved"
    ARCHIVED = "archived"


class EventType(str, Enum):
    """Every meaningful action writes exactly one of these.

    The event log is the source of truth — if it's not here, it didn't happen.
    """

    TASK_CREATED = "task.created"
    REPO_CHECKED = "repo.checked"
    WORKTREE_CREATED = "worktree.created"
    CONTEXT_BUILT = "context.built"
    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    COMMAND_STARTED = "command.started"
    COMMAND_FINISHED = "command.finished"
    DIFF_CAPTURED = "diff.captured"
    REVIEW_COMPLETED = "review.completed"
    REPORT_WRITTEN = "report.written"
    VAULT_NOTE_WRITTEN = "vault.note_written"
    HUMAN_APPROVED = "human.approved"
    MEMORY_PROMOTED = "memory.promoted"
    TASK_FAILED = "task.failed"
    TASK_COMPLETED = "task.completed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Recommendation(str, Enum):
    MERGE = "merge"
    REVISE = "revise"
    REJECT = "reject"


class MemoryStatus(str, Enum):
    """Synthadoc-style 5-state lifecycle for vault notes."""

    DRAFT = "draft"
    ACTIVE = "active"
    STALE = "stale"
    CONTRADICTED = "contradicted"
    ARCHIVED = "archived"


# --------------------------------------------------------------------------- #
# Core models
# --------------------------------------------------------------------------- #


class Task(BaseModel):
    """A single coding task. One task → one worktree → one branch → one report."""

    task_id: str
    repo_name: str
    repo_path: Path
    base_branch: str
    task_branch: str
    worktree_path: Path
    user_request: str
    status: TaskStatus = TaskStatus.CREATED
    created_at: str = Field(default_factory=_utcnow_iso)
    updated_at: str = Field(default_factory=_utcnow_iso)

    def touch(self) -> None:
        """Stamp updated_at. Call after any status change."""
        self.updated_at = _utcnow_iso()


class Event(BaseModel):
    """One append-only log line. The atom of truth."""

    event_id: str
    task_id: str
    type: EventType
    timestamp: str = Field(default_factory=_utcnow_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


class CommandResult(BaseModel):
    """Outcome of one configured command (install/lint/typecheck/test/build)."""

    command: str
    cwd: Path
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    duration_seconds: float
    skipped: bool = False  # True when the command was empty/disabled in config

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class AgentResult(BaseModel):
    """Outcome of one agent run."""

    agent_name: str
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    summary: str = "Agent completed"

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class ReviewResult(BaseModel):
    """The reviewer's verdict on a captured diff. Advisory; humans decide."""

    risk: RiskLevel
    recommendation: Recommendation
    changed_files: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    summary: str = ""
    hard_block: bool = False  # True → auto-reject regardless of risk wording


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def next_event_id(existing_count: int) -> str:
    """Monotonic, zero-padded event id, e.g. evt_000001."""
    return f"evt_{existing_count + 1:06d}"
