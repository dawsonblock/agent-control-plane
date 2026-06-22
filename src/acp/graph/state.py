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
    config: Any            # RepoConfig — opaque to LangGraph's typing
    user_request: str
    vault_root: Path
    runs_root: Path

    # --- accumulated as the run progresses ------------------------------ #
    task_id: str
    task: Any              # Task — kept here so nodes can read/update it
    status: TaskStatus
    repo_path: Path
    worktree_path: Path
    artifacts_dir: Path

    context_bundle_path: Path | None   # None until M6 (Haystack)
    prompt_path: Path
    agent_result: AgentResult
    command_results: list[CommandResult]
    review_result: ReviewResult
    diff: Any              # DiffCapture

    report_path: Path
    vault_note_path: Path

    # --- failure tracking (for the M4 repair loop and reports) ---------- #
    error: str             # human-readable reason if status == FAILED
    failure_event: EventType  # which final event to write (task.failed / task.completed)


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
