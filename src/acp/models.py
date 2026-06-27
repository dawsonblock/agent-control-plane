"""Typed data models — the shared vocabulary of the control plane.

Everything that crosses a layer boundary is one of these types. They match
the JSON shapes in docs/architecture.md and the spec's data-model section.
Events and reports are truth; these models enforce that truth is well-formed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with a trailing Z, e.g. 2026-06-21T12:00:00Z."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    # v0.5.16: REJECTED is a first-class human decision, distinct from
    # ARCHIVED (which is a later cleanup state). A rejected task is
    # explicitly not approved; an archived task may be a cleanup of
    # any terminal state.
    REJECTED = "rejected"
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
    # v0.5.13+: Docker Sandboxes executor backend.
    # sandbox.configured = validated but not yet started (intention, not fact)
    # sandbox.started    = sbx actually launched successfully (fact)
    # sandbox.failed     = sbx run failed to start or crashed
    # sandbox.stopped    = sandbox stopped/removed after cleanup
    SANDBOX_CONFIGURED = "sandbox.configured"
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_FAILED = "sandbox.failed"
    SANDBOX_STOPPED = "sandbox.stopped"
    # v0.6.0: Autonomous mode — programmatic approval + merge.
    # auto.approved = gates passed in autonomous mode, no human click
    # auto.merged   = task branch merged into default branch
    # auto.merge.refused = auto-merge refused (high risk or tampered event chain)
    # test_generation.attempted = repair loop switched to test-writing mode
    AUTO_APPROVED = "auto.approved"
    AUTO_MERGED = "auto.merged"
    AUTO_MERGE_REFUSED = "auto.merge.refused"
    TEST_GENERATION_ATTEMPTED = "test_generation.attempted"
    # v0.6.9: Agent federation via MCP.
    # federation.discovered = MCP server tools discovered before agent run
    # federation.tool_called  = a federated tool was called (proxied by ACP)
    FEDERATION_DISCOVERED = "federation.discovered"
    FEDERATION_TOOL_CALLED = "federation.tool_called"
    # v0.7.0 (M14): Mission layer — group tasks into larger epics.
    # mission.created   = a new mission was defined from a YAML goal
    # mission.completed = all steps in a mission finished (approved or aborted)
    MISSION_CREATED = "mission.created"
    MISSION_COMPLETED = "mission.completed"
    # v0.7.0+: Forward-declared event types for upcoming layers.
    # These are defined now (Layer 0 schema) so downstream features can
    # emit and verify them without waiting for a models.py change.
    # Phase 1.1: SQLite becomes primary task state truth.
    TASK_STORE_MIGRATED = "task.store_migrated"
    # Phase 1.3: Secret hard-block in review_diff.
    REVIEW_SECRET_HARD_BLOCK = "review.secret_hard_block"
    # Phase 2.1: Jailed executor (gVisor/OpenHands).
    EXECUTOR_JAILS_CREATED = "executor.jails_created"
    EXECUTOR_JAILED_RUN_FINISHED = "executor.jailed_run_finished"
    # Phase 3.1: HTTP/SSE MCP transport.
    FEDERATION_SERVER_CONNECTED = "federation.server_connected"
    # Phase 3.2: Sub-task spawning (agent-to-agent).
    TASK_SUBTASK_SPAWNED = "task.subtask_spawned"
    # Phase 4.1: Semantic memory garbage collection.
    MEMORY_PRUNED = "memory.pruned"
    # v0.7.1: SQLite integrity breach — task.json and SQLite disagree.
    STORE_INTEGRITY_BREACH = "store.integrity_breach"
    # v0.7.1: Autonomous mode — repair loop aborted by circuit breaker.
    AUTO_REPAIR_LOOP_ABORTED = "auto.repair_loop_aborted"
    # v0.7.3: Mid-stream sentinel — agent killed during execution by the
    # StreamSentinel for safety (secret leak) or attractor (strange loop).
    # The payload includes "reason" (secret_detected | strange_loop) and
    # "chunk_preview" of the offending output.
    STREAM_ABORTED = "stream.aborted"


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
    # v0.7.4: Recursion depth for subtask spawning. Root tasks have
    # depth=0. Each subtask level increments by 1. Hard-capped at
    # MAX_SUBTASK_RECURSION_DEPTH to prevent agent fork bombs where
    # an agent recursively delegates to subtasks indefinitely.
    recursion_depth: int = 0

    def touch(self) -> None:
        """Stamp updated_at. Call after any status change."""
        self.updated_at = _utcnow_iso()


# v0.7.4: Maximum subtask recursion depth. A task at depth N can spawn
# subtasks at depth N+1, but only if N+1 <= this limit. This prevents
# unbounded recursive subtask spawning (agent fork bombs) where an agent
# delegates to a subtask, which delegates to a subtask, etc.
MAX_SUBTASK_RECURSION_DEPTH = 3


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
    prev_hash: str = ""  # hash of the preceding event; "GENESIS" for the first
    hash: str = ""  # sha256 of (prev_hash + event_id + task_id + type + timestamp + payload)
    signature: str = ""  # Ed25519 signature over `hash` (hex); empty if unsigned


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
    # v0.7.3: Set by the streaming CLIAgent when the StreamSentinel killed
    # the agent mid-execution (secret leak, strange loop, dangerous path).
    # The graph uses this to short-circuit to `failed` instead of running
    # tests and review on a partial, potentially dangerous diff.
    aborted_by_sentinel: bool = False
    # v0.7.3: When aborted_by_sentinel is True, this carries the abort
    # reason ("secret_detected", "strange_loop", "dangerous_path") for
    # the agent.finished event payload.
    sentinel_abort_reason: str = ""

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
# Mission layer (v0.7.0 / M14)
# --------------------------------------------------------------------------- #


class MissionStatus(str, Enum):
    """Lifecycle of a mission. A mission groups sequential tasks toward a goal."""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"


class MissionStep(BaseModel):
    """One step in a mission — becomes a single ACP task run when spawned.

    Steps are sequential: step N+1 can read the artifacts of step N (cross-
    task artifact sharing, Phase 5.2). A step is ``pending`` until ACP
    spawns a task for it, ``running`` while that task is active, and
    ``completed``/``failed`` once the task reaches a terminal state.
    """

    description: str
    task_id: str = ""  # filled in when acp spawns the task for this step
    status: str = "pending"  # pending | running | completed | failed


class Mission(BaseModel):
    """A mission — an overarching goal split into sequential task runs.

    A mission is defined from a YAML goal (e.g. "Migrate to React 19").
    ACP splits it into ordered :class:`MissionStep` entries, each of which
    becomes a single task run. The mission directory
    ``data/missions/<mission_id>/`` holds ``mission.yaml`` (canonical
    state) and ``events.jsonl`` (mission-level event log).
    """

    mission_id: str
    goal: str
    description: str = ""
    repo_name: str
    repo_path: Path
    base_branch: str = "main"
    steps: list[MissionStep] = Field(default_factory=list)
    status: MissionStatus = MissionStatus.CREATED
    created_at: str = Field(default_factory=_utcnow_iso)
    updated_at: str = Field(default_factory=_utcnow_iso)
    completed_at: str = ""

    def touch(self) -> None:
        """Stamp updated_at. Call after any status change."""
        self.updated_at = _utcnow_iso()


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
    from acp.review.gates import GateOutcome, evaluate_final_gates

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
