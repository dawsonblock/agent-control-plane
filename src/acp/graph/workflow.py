"""The compiled ACP LangGraph workflow.

Wires the node adapters from ``nodes.py`` into a ``StateGraph`` with
conditional edges that route failures to the ``failed`` node. The happy
path is linear:

    START → create_task → check_repo → create_worktree → build_context
         → run_agent → run_tests → capture_diff → review_diff
         → write_report → write_vault_note → done → END

Failure short-circuits route to ``failed`` instead. The ``failed`` node
still writes a report when it can (spec rule: a failed task produces an
evidence report) — except for the pre-worktree dirty-repo case, where
nothing exists to report on yet.

Compiled with an in-memory ``MemorySaver`` checkpointer so runs are
inspectable. (Durable checkpointing is a later concern; M3 only needs the
graph to be drivable and its transitions observable.)
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from acp.events import EventWriter
from acp.graph.nodes import (
    NodeContext,
    build_context_node,
    capture_diff_node,
    check_repo,
    create_task,
    create_worktree_node,
    done_node,
    failed_node,
    review_diff_node,
    run_agent_node,
    run_tests_node,
    write_report_node,
    write_vault_note_node,
)
from acp.graph.state import ACPState
from acp.models import TaskStatus
from acp.store import TaskStore


def _is_failed(state: dict[str, Any]) -> bool:
    """Conditional-edge router: did the preceding node mark the run failed?"""
    return state.get("status") == TaskStatus.FAILED


def _route_after_check(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "create_worktree"


def _route_after_worktree(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "build_context"


def build_workflow(
    *,
    store: TaskStore,
    events: EventWriter,
) -> Any:
    """Build + compile the ACP workflow graph.

    The ``store`` and ``events`` are bound to each node via ``NodeContext``
    so every node shares the same run dir + event log. The ``events`` writer
    may be constructed with a placeholder task id; the ``create_task`` node
    relocates it to the real run dir once the id is minted.

    Returns a compiled graph ready to ``.invoke(initial_state)``.
    """
    ctx = NodeContext(store=store, events=events)

    g = StateGraph(ACPState)

    # Bind ctx into each node so LangGraph sees a single-arg callable.
    g.add_node("create_task", partial(create_task, ctx=ctx))
    g.add_node("check_repo", partial(check_repo, ctx=ctx))
    g.add_node("create_worktree", partial(create_worktree_node, ctx=ctx))
    g.add_node("build_context", partial(build_context_node, ctx=ctx))
    g.add_node("run_agent", partial(run_agent_node, ctx=ctx))
    g.add_node("run_tests", partial(run_tests_node, ctx=ctx))
    g.add_node("capture_diff", partial(capture_diff_node, ctx=ctx))
    g.add_node("review_diff", partial(review_diff_node, ctx=ctx))
    g.add_node("write_report", partial(write_report_node, ctx=ctx))
    g.add_node("write_vault_note", partial(write_vault_note_node, ctx=ctx))
    g.add_node("done", partial(done_node, ctx=ctx))
    g.add_node("failed", partial(failed_node, ctx=ctx))

    # --- entry + linear happy path -------------------------------------- #
    g.add_edge(START, "create_task")
    g.add_edge("create_task", "check_repo")

    # check_repo → failed (dirty) OR create_worktree
    g.add_conditional_edges("check_repo", _route_after_check)

    # create_worktree → failed (error) OR build_context
    g.add_conditional_edges("create_worktree", _route_after_worktree)

    g.add_edge("build_context", "run_agent")
    g.add_edge("run_agent", "run_tests")
    g.add_edge("run_tests", "capture_diff")
    g.add_edge("capture_diff", "review_diff")
    g.add_edge("review_diff", "write_report")
    g.add_edge("write_report", "write_vault_note")
    g.add_edge("write_vault_note", "done")

    # Terminal nodes.
    g.add_edge("done", END)
    g.add_edge("failed", END)

    return g.compile(checkpointer=MemorySaver())


# --------------------------------------------------------------------------- #
# Convenience runner — used by the CLI (and tests).
# --------------------------------------------------------------------------- #

def run_workflow(
    *,
    config: Any,
    user_request: str,
    runs_root: Path | str,
    vault_root: Path | str,
) -> dict[str, Any]:
    """Build + invoke the graph once and return the final state.

    Handles the placeholder-writer setup: the EventWriter is constructed with
    a sentinel id, and the ``create_task`` node relocates it to the real run
    dir once the task id is minted. Returns the graph's final state dict.
    """
    store = TaskStore(runs_root=runs_root)
    # Placeholder writer — create_task will relocate it to the real run dir.
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)

    state = {
        "config": config,
        "user_request": user_request,
        "vault_root": Path(vault_root),
        "runs_root": Path(runs_root),
    }
    return wf.invoke(state, config={"configurable": {"thread_id": "acp-run"}})
