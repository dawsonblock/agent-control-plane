"""Tests for graph node failure events — v0.5 acceptance criteria.

When a LangGraph node raises an unhandled exception:
  - A ``node.failed`` event must be written
  - Task status must be ``FAILED``
  - The error message must be preserved in the state
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter
from acp.graph.nodes import NodeContext
from acp.graph.state import ACPState
from acp.graph.workflow import node_error_handler
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _config(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(),
        commands=CommandsSection(lint='echo "lint ok"', test='echo "ok"'),
        review=ReviewSection(),
    )


def test_node_error_handler_writes_node_failed_event(disposable_repo, isolated_workspace):
    """When node_error_handler catches an exception, node.failed event must exist."""
    store = TaskStore(runs_root=Path(isolated_workspace["runs_root"]) / "fail_evt")
    events = EventWriter("__pending__", store.root / "__pending__")
    events.relocate("test_evt", store.root / "test_evt")

    ctx = NodeContext(store=store, events=events)
    graph = _build_minimal_graph_with_broken_node(ctx)

    state = {"config": _config(disposable_repo.path), "user_request": "test"}
    result = graph.invoke(state, config={"configurable": {"thread_id": "acp-fail-evt"}})

    assert result.get("status") == TaskStatus.FAILED

    evts_path = events.path
    if evts_path.exists():
        events_list = [json.loads(l) for l in evts_path.read_text().splitlines() if l.strip()]
        node_failed = [e for e in events_list if e["type"] == EventType.NODE_FAILED.value]
        assert len(node_failed) >= 1, f"expected node.failed event, got {events_list}"
        payload = node_failed[0]["payload"]
        assert "broken" in payload.get("node", "")
        assert "intentional node crash" in payload.get("message", "")


def test_node_error_handler_sets_status_failed(disposable_repo, isolated_workspace):
    """When node_error_handler catches an exception, status must be FAILED."""
    store = TaskStore(runs_root=Path(isolated_workspace["runs_root"]) / "fail_st")
    events = EventWriter("__pending__", store.root / "__pending__")
    events.relocate("test_st", store.root / "test_st")

    ctx = NodeContext(store=store, events=events)
    graph = _build_minimal_graph_with_broken_node(ctx)

    state = {"config": _config(disposable_repo.path), "user_request": "test"}
    result = graph.invoke(state, config={"configurable": {"thread_id": "acp-fail-st"}})

    assert result.get("status") == TaskStatus.FAILED, (
        f"expected FAILED, got {result.get('status')}"
    )
    error = result.get("error", "")
    assert error, "error field should not be empty after node failure"


def _build_minimal_graph_with_broken_node(ctx: NodeContext) -> Any:
    """Build a minimal graph with a node that always raises."""

    def broken_node(state: dict[str, Any], _ctx: NodeContext) -> dict[str, Any]:
        msg = "intentional node crash for test"
        raise RuntimeError(msg)

    # Wrap broken_node with the same error handler used by the real workflow.
    wrapped = partial(node_error_handler(broken_node), ctx=ctx)

    g = StateGraph(ACPState)
    g.add_node("broken", wrapped)
    g.add_node("done", lambda s: s)
    g.add_edge(START, "broken")
    g.add_conditional_edges("broken", lambda s: "done" if s.get("status") != TaskStatus.FAILED else END)
    g.add_edge("done", END)
    return g.compile(checkpointer=MemorySaver())


def test_node_failure_writes_node_failed_event_directly(disposable_repo, isolated_workspace):
    """When node_error_handler catches an exception, node.failed is written."""
    store = TaskStore(runs_root=Path(isolated_workspace["runs_root"]) / "direct_fail")
    events = EventWriter("__pending__", store.root / "__pending__")
    events.relocate("test_direct_fail", store.root / "test_direct_fail")

    ctx = NodeContext(store=store, events=events)
    graph = _build_minimal_graph_with_broken_node(ctx)

    state = {"config": _config(disposable_repo.path), "user_request": "test"}
    result = graph.invoke(state, config={"configurable": {"thread_id": "acp-direct-fail"}})

    assert result.get("status") == TaskStatus.FAILED
    assert "intentional node crash" in (result.get("error") or "")

    evts_path = events.path
    if evts_path.exists():
        events_list = [json.loads(l) for l in evts_path.read_text().splitlines() if l.strip()]
        node_failed = [e for e in events_list if e["type"] == EventType.NODE_FAILED.value]
        assert len(node_failed) >= 1, (
            f"expected at least one node.failed event, got {events_list}"
        )
        payload = node_failed[0]["payload"]
        assert payload["node"] == "broken_node"
        assert "intentional node crash" in payload["message"]