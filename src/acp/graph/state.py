"""Workflow state — the TypedDict that flows between graph nodes.

This is the LangGraph state object (spec §4 Phase 4). Each node reads what it
needs and returns a *patch* (a partial dict) that LangGraph merges in. The
``status`` field carries the TaskStatus so the graph's conditional edges can
branch on it (dirty repo → failed, failing test → failed, etc.).

Note: this is a *runtime* state, distinct from the persisted ``Task`` model.
``Task`` (with task.json) is the durable record; ``ACPState`` is the ephemeral
data the graph passes around during a single invocation. Both stay in sync
because nodes persist to the store as they go.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from acp.models import (
    AgentResult,
    CommandResult,
    EventType,
    ReviewResult,
    TaskStatus,
)


class ACPState(TypedDict, total=False):
    """Ephemeral per-run workflow state.

    All fields are optional because the graph builds them up node by node.
    ``total=False`` lets each node return only the keys it touched.
    """

    # --- inputs (set at invocation) ------------------------------------- #
    config: Any  # RepoConfig — opaque to LangGraph's typing
    user_request: str
    vault_root: Path
    runs_root: Path
    preallocated_task_id: str  # when set, create_task reuses it

    # --- accumulated as the run progresses ------------------------------ #
    task_id: str
    task: Any  # Task — kept here so nodes can read/update it
    status: TaskStatus
    repo_path: Path
    worktree_path: Path
    artifacts_dir: Path

    context_bundle_path: Path | None  # None until M6 (Haystack)
    prompt_path: Path
    agent_result: AgentResult
    command_results: list[CommandResult]
    review_result: ReviewResult
    diff: Any  # DiffCapture

    report_path: Path
    vault_note_path: Path

    # --- gate result (v0.5.2: first-class final gate artifact) ---------- #
    gate_result: Any  # GateResult — stored by write_report_node, consumed
    # by terminal nodes and report rendering.

    # --- evidence manifest (v0.5.5: tamper-evident evidence) ------------- #
    manifest_hash: str  # sha256 over the evidence manifest; included in
    # the report so report ↔ evidence binding is
    # verifiable.

    # --- failure tracking (for the M4 repair loop and reports) ---------- #
    error: str  # human-readable reason if status == FAILED
    failure_event: EventType  # which final event to write (task.failed / task.completed)

    # --- M4 repair loop ------------------------------------------------- #
    # How many repair attempts have run so far (0 = none). Bounded by
    # config.agent.max_repair_attempts so the graph can't loop forever.
    repair_attempts: int
    # Per-attempt record: {attempt, prompt_path, failures_fixed, still_failing}
    repair_history: list[dict[str, Any]]

    # --- v0.5.13: Docker Sandboxes executor ----------------------------- #
    sandbox_name: str  # acp-<task_id> when executor is docker_sbx
    sandbox_remote: str  # sandbox-acp-<task_id> on the host
    sandbox_metadata: dict[str, Any]  # recorded in sandbox.started event

    # --- v0.6.0: Autonomous mode ---------------------------------------- #
    # Whether autonomous mode was active for this run.
    auto_approved: bool  # True if auto.approved was written
    auto_merged: bool  # True if auto.merged was written
    merge_commit_sha: str  # SHA of the merge commit (if any)
    # Circuit breaker: fingerprints of previous repair failure signatures.
    # Used to detect when the agent is repeating the same fix.
    repair_fingerprints: list[str]

    # --- v0.7.0 (M14): Mission context — cross-task artifact sharing ---- #
    # When a task is spawned as part of a mission step, this holds the
    # context needed to bind the task to the mission and reference the
    # preceding task's artifacts. The parent_task_id is the task that ran
    # for the previous mission step; its diff.patch hash is included in
    # evidence.finalized, proving this task was generated sequentially.
    mission_id: str  # the mission this task belongs to
    mission_step_index: int  # which step in the mission (0-based)
    parent_task_id: str  # preceding step's task_id ("" for step 0)

    # --- v0.7.4: Subtask recursion depth — prevents agent fork bombs ------ #
    # Root tasks have depth=0. Each subtask level increments by 1.
    # Hard-capped at MAX_SUBTASK_RECURSION_DEPTH to prevent unbounded
    # recursive subtask spawning.
    recursion_depth: int


def initial_state(
    *,
    config: Any,
    user_request: str,
    vault_root: Path,
    runs_root: Path,
) -> ACPState:
    """Build the minimal starting state for a graph invocation."""
    return ACPState(
        config=config,
        user_request=user_request,
        vault_root=Path(vault_root),
        runs_root=Path(runs_root),
    )
