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
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from acp.agents.base import AgentProtocol
from acp.agents.registry import build_agent as _default_build_agent
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
    repair_plan_node,
    review_diff_node,
    run_agent_node,
    run_repair_agent_node,
    run_tests_node,
    write_report_node,
    write_vault_note_node,
)
from acp.graph.state import ACPState
from acp.models import TaskStatus
from acp.store import TaskStore
from acp.testing.runner import all_passed


def _is_failed(state: dict[str, Any]) -> bool:
    """Conditional-edge router: did the preceding node mark the run failed?"""
    return state.get("status") == TaskStatus.FAILED


def _route_after_check(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "create_worktree"


def _route_after_worktree(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "build_context"


def _route_after_tests(state: dict[str, Any]) -> str:
    """Route after run_tests: repair if failing + attempts remain, else proceed.

    This is the M4 repair loop's cap. When ``max_repair_attempts`` is 0 the
    behavior is identical to M3 — a failing test falls straight through to
    capture_diff → review → FAILED report.
    """
    cfg = state.get("config")
    if all_passed(state.get("command_results", [])):
        return "capture_diff"
    if cfg is None:
        return "capture_diff"
    attempts = int(state.get("repair_attempts", 0))
    if attempts < cfg.agent.max_repair_attempts:
        return "repair_plan"
    return "capture_diff"


def build_workflow(
    *,
    store: TaskStore,
    events: EventWriter,
    agent_factory: Callable[[Any], Any] | None = None,
) -> Any:
    """Build + compile the ACP workflow graph.

    The ``store`` and ``events`` are bound to each node via ``NodeContext``
    so every node shares the same run dir + event log. The ``events`` writer
    may be constructed with a placeholder task id; the ``create_task`` node
    relocates it to the real run dir once the id is minted.

    ``agent_factory`` is optional and defaults to the registry's
    ``build_agent``; tests inject a controllable agent to exercise the
    repair loop deterministically.

    Returns a compiled graph ready to ``.invoke(initial_state)``.
    """
    ctx = NodeContext(
        store=store,
        events=events,
        agent_factory=agent_factory or _default_build_agent,
    )

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
    # M4 repair loop.
    g.add_node("repair_plan", partial(repair_plan_node, ctx=ctx))
    g.add_node("run_repair", partial(run_repair_agent_node, ctx=ctx))

    # --- entry + linear happy path -------------------------------------- #
    g.add_edge(START, "create_task")
    g.add_edge("create_task", "check_repo")

    # check_repo → failed (dirty) OR create_worktree
    g.add_conditional_edges("check_repo", _route_after_check)

    # create_worktree → failed (error) OR build_context
    g.add_conditional_edges("create_worktree", _route_after_worktree)

    g.add_edge("build_context", "run_agent")
    g.add_edge("run_agent", "run_tests")

    # run_tests → repair_plan (if failing + attempts remain) OR capture_diff.
    # The repair loop: repair_plan → run_repair → run_tests (re-evaluated).
    g.add_conditional_edges("run_tests", _route_after_tests)
    g.add_edge("repair_plan", "run_repair")
    g.add_edge("run_repair", "run_tests")

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
    agent_factory: Callable[[Any], AgentProtocol] | None = None,
) -> dict[str, Any]:
    """Build + invoke the graph once and return the final state.

    Handles the placeholder-writer setup: the EventWriter is constructed with
    a sentinel id, and the ``create_task`` node relocates it to the real run
    dir once the task id is minted. Returns the graph's final state dict.
    """
    store = TaskStore(runs_root=runs_root)
    # Placeholder writer — create_task will relocate it to the real run dir.
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events, agent_factory=agent_factory)

    state = {
        "config": config,
        "user_request": user_request,
        "vault_root": Path(vault_root),
        "runs_root": Path(runs_root),
    }
    return wf.invoke(state, config={"configurable": {"thread_id": "acp-run"}})
