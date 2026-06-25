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
    EXECUTING = "executing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
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
    HUMAN_REJECTED = "human.rejected"
    MEMORY_PROMOTED = "memory.promoted"
    # M4 repair loop.
    REPAIR_ATTEMPTED = "repair.attempted"
    REPAIR_EXHAUSTED = "repair.exhausted"
    TASK_NEEDS_REVIEW = "task.needs_review"
    TASK_FAILED = "task.failed"
    TASK_COMPLETED = "task.completed"
    NODE_FAILED = "node.failed"
    EVIDENCE_FINALIZED = "evidence.finalized"
    EVIDENCE_REPORT_BOUND = "evidence.report_bound"
    # v0.5.13: Docker Sandboxes executor backend.
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_STOPPED = "sandbox.stopped"


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
    base_commit_sha: str = ""
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
    """One append-only log line. The atom of truth.

    Events form a hash chain: each event includes ``prev_hash`` (the hash of
    the preceding event, or ``GENESIS`` for the first event) and ``hash``
    (sha256 of ``prev_hash + event_id + task_id + type + timestamp + payload``).
    This makes the log tamper-evident — removing, reordering, or modifying any
    event breaks the chain.

    An optional ``signature`` field (v0.5.6) holds an Ed25519 signature over
    the event's hash, proving authenticity (who wrote the log) in addition to
    integrity (the log hasn't been modified). Empty when no signing key is
    configured.
    """

    event_id: str
    task_id: str
    type: EventType
    timestamp: str = Field(default_factory=_utcnow_iso)
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""   # hash of the preceding event; "GENESIS" for the first
    hash: str = ""        # sha256 of (prev_hash + event_id + task_id + type + timestamp + payload)
    signature: str = ""   # Ed25519 signature over `hash` (hex); empty if unsigned


class CommandResult(BaseModel):
    """Outcome of one configured command (install/lint/typecheck/test/build)."""

    command: str
    cwd: Path
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    duration_seconds: float
    skipped: bool = False  # True when the command was empty/disabled in config
    timed_out: bool = False  # True when the command was killed by timeout

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


# --------------------------------------------------------------------------- #
# Gate-correct final-status computation (v0.5)
# --------------------------------------------------------------------------- #


def compute_final_status(
    *,
    agent_passed: bool,
    command_results: list[CommandResult],
    diff_changed_files: list[str],
    review: ReviewResult | None,
) -> TaskStatus:
    """Determine the final task status using gate-correct logic.

    Delegates to ``acp.review.gates.evaluate_final_gates`` which is the
    single source of truth for gate outcomes. This function remains for
    backward compatibility; new code should call ``evaluate_final_gates``
    directly for richer results.
    """
    from acp.review.gates import evaluate_final_gates, GateOutcome

    agent_exit_code = 0 if agent_passed else 1

    result = evaluate_final_gates(
        agent_exit_code=agent_exit_code,
        command_results=command_results,
        review_result=review,
        changed_files=diff_changed_files,
    )

    if result.outcome == GateOutcome.PASSED:
        return TaskStatus.PASSED
    if result.outcome == GateOutcome.NEEDS_REVIEW:
        return TaskStatus.NEEDS_REVIEW
    return TaskStatus.FAILED
