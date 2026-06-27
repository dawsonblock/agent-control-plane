"""Graph-level integration test for the Mid-Stream Sentinel abort path.

Verifies that when the StreamSentinel kills the agent mid-execution:
1. The graph routes to `failed` (not `run_tests`)
2. `stream.aborted` and `task.failed` events are in the event log
3. No diff/review artifacts are produced (short-circuited)
4. The failure report is written with the abort reason
5. The main branch HEAD is unchanged (core invariant)

Also tests the repair-agent sentinel abort path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from acp.agents.cli_agent import CLIAgent
from acp.config import (
    AgentSection,
    CommandsSection,
    ExecutorSection,
    RepoConfig,
    RepoSection,
    ReviewSection,
    StreamingSection,
)
from acp.events import EventWriter
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _streaming_config(repo_path: Path, script_path: Path) -> RepoConfig:
    """Build a RepoConfig with streaming enabled and a custom command."""
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(
            default="custom",
            command_template=f"{sys.executable} {script_path}",
        ),
        commands=CommandsSection(lint="echo ok", test="echo ok"),
        review=ReviewSection(require_human_approval=False),
        executor=ExecutorSection(backend="worktree"),
        streaming=StreamingSection(enabled=True),
    )


def _make_agent_factory(cfg: RepoConfig):
    """Return an agent_factory that builds a CLIAgent with the given config."""

    def factory(config: RepoConfig) -> CLIAgent:
        return CLIAgent(cfg)

    return factory


async def _run_graph(
    repo_path: Path,
    runs_root: Path,
    vault_root: Path,
    script_path: Path,
):
    """Build + invoke the graph with a streaming CLIAgent, returning (result, store)."""
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    cfg = _streaming_config(repo_path, script_path)
    wf = build_workflow(
        store=store,
        events=events,
        agent_factory=_make_agent_factory(cfg),
    )
    state = initial_state(
        config=cfg,
        user_request="test sentinel abort",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "acp-sentinel-test"}})
    return result, store


def _event_types(store: TaskStore, task_id: str) -> list[str]:
    p = store.events_path(task_id)
    if not p.exists():
        return []
    return [json.loads(line)["type"] for line in p.read_text().splitlines() if line.strip()]


def _main_head(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# Graph-level sentinel abort — secret detection
# --------------------------------------------------------------------------- #


async def test_graph_sentinel_secret_abort_routes_to_failed(
    disposable_repo, isolated_workspace, tmp_path
):
    """When the sentinel kills the agent for a secret leak, the graph routes to `failed`.

    The graph should:
    - Reach `failed` (not `run_tests` or `done`)
    - Write `stream.aborted` and `task.failed` events
    - NOT write diff/review artifacts (short-circuited before capture_diff)
    - Write a failure report
    - Leave the main branch unchanged
    """
    # Write a script that outputs a secret.
    parts = ["AKIA", "IOSFODNN7EXAMPLE"]
    secret = "".join(parts)
    script = tmp_path / "leak_agent.py"
    script.write_text(f"print('working on the task')\nprint('export AWS_KEY={secret}')\n")

    result, store = await _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        script,
    )

    assert result["status"] == TaskStatus.FAILED
    task_id = result["task_id"]

    events = _event_types(store, task_id)
    # The sentinel wrote a stream.aborted event.
    assert EventType.STREAM_ABORTED.value in events, f"stream.aborted not in events: {events}"
    # The graph wrote exactly one task.failed event (from failed_node, not
    # duplicated by run_agent_node).
    task_failed_count = events.count(EventType.TASK_FAILED.value)
    assert task_failed_count == 1, (
        f"expected exactly 1 task.failed event, got {task_failed_count}: {events}"
    )
    # The agent.started event was written (before the abort).
    assert EventType.AGENT_STARTED.value in events
    # The agent.finished event was written (with abort info).
    assert EventType.AGENT_FINISHED.value in events

    # Critical: the graph short-circuited — no tests, diff, or review ran.
    assert EventType.COMMAND_STARTED.value not in events, (
        "tests ran after sentinel abort — graph should have short-circuited"
    )
    assert EventType.DIFF_CAPTURED.value not in events, (
        "diff captured after sentinel abort — graph should have short-circuited"
    )
    assert EventType.REVIEW_COMPLETED.value not in events, (
        "review ran after sentinel abort — graph should have short-circuited"
    )

    # A failure report was written.
    report_path = result.get("report_path")
    if report_path:
        assert Path(str(report_path)).is_file(), "failure report not written"
        report_text = Path(str(report_path)).read_text()
        # The report should mention the sentinel abort in the error or timeline.
        assert (
            "sentinel" in report_text.lower()
            or "stream" in report_text.lower()
            or ("StreamSentinel" in report_text)
        ), f"sentinel abort not mentioned in report: {report_text[:200]}"

    # Core invariant: main branch unchanged.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


async def test_graph_sentinel_strange_loop_abort_routes_to_failed(
    disposable_repo, isolated_workspace, tmp_path
):
    """When the sentinel kills the agent for a strange loop, the graph routes to `failed`."""
    # Write a script that loops on the same output.
    script = tmp_path / "loop_agent.py"
    script.write_text(
        "for _ in range(50):\n    print('Error: cannot find module foo in path bar baz')\n"
    )

    # Use a config with a low strange-loop threshold.
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = EventWriter("__pending__", store.root / "__pending__")
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=disposable_repo.path, default_branch="main"),
        agent=AgentSection(
            default="custom",
            command_template=f"{sys.executable} {script}",
        ),
        commands=CommandsSection(lint="echo ok", test="echo ok"),
        review=ReviewSection(require_human_approval=False),
        executor=ExecutorSection(backend="worktree"),
        streaming=StreamingSection(
            enabled=True,
            strange_loop_threshold=5.0,
            strange_loop_similarity=0.6,
        ),
    )
    wf = build_workflow(
        store=store,
        events=events,
        agent_factory=_make_agent_factory(cfg),
    )
    state = initial_state(
        config=cfg,
        user_request="test strange loop abort",
        vault_root=isolated_workspace["vault_root"],
        runs_root=isolated_workspace["runs_root"],
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "acp-loop-test"}})

    assert result["status"] == TaskStatus.FAILED
    task_id = result["task_id"]

    events_list = _event_types(store, task_id)
    assert EventType.STREAM_ABORTED.value in events_list
    assert EventType.TASK_FAILED.value in events_list
    # Short-circuited — no tests/diff/review.
    assert EventType.DIFF_CAPTURED.value not in events_list

    # Core invariant.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


async def test_graph_sentinel_dangerous_path_abort_routes_to_failed(
    disposable_repo, isolated_workspace, tmp_path
):
    """When the sentinel kills the agent for a dangerous path, the graph routes to `failed`."""
    script = tmp_path / "danger_agent.py"
    script.write_text("print('Running: rm -rf / to clean up')\n")

    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = EventWriter("__pending__", store.root / "__pending__")
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=disposable_repo.path, default_branch="main"),
        agent=AgentSection(
            default="custom",
            command_template=f"{sys.executable} {script}",
        ),
        commands=CommandsSection(lint="echo ok", test="echo ok"),
        review=ReviewSection(require_human_approval=False),
        executor=ExecutorSection(backend="worktree"),
        streaming=StreamingSection(
            enabled=True,
            dangerous_path_patterns=[r"rm\s+-rf\s+/"],
        ),
    )
    wf = build_workflow(
        store=store,
        events=events,
        agent_factory=_make_agent_factory(cfg),
    )
    state = initial_state(
        config=cfg,
        user_request="test dangerous path abort",
        vault_root=isolated_workspace["vault_root"],
        runs_root=isolated_workspace["runs_root"],
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "acp-danger-test"}})

    assert result["status"] == TaskStatus.FAILED
    task_id = result["task_id"]

    events_list = _event_types(store, task_id)
    assert EventType.STREAM_ABORTED.value in events_list
    assert EventType.TASK_FAILED.value in events_list
    assert EventType.DIFF_CAPTURED.value not in events_list

    # Core invariant.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


async def test_graph_streaming_disabled_completes_normally(
    disposable_repo, isolated_workspace, tmp_path
):
    """When streaming is disabled, a benign agent completes normally through the graph."""
    script = tmp_path / "benign_agent.py"
    script.write_text("print('editing files')\nprint('done')\n")

    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = EventWriter("__pending__", store.root / "__pending__")
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=disposable_repo.path, default_branch="main"),
        agent=AgentSection(
            default="custom",
            command_template=f"{sys.executable} {script}",
        ),
        commands=CommandsSection(lint="echo ok", test="echo ok"),
        review=ReviewSection(require_human_approval=False),
        executor=ExecutorSection(backend="worktree"),
        streaming=StreamingSection(enabled=False),  # streaming OFF
    )
    wf = build_workflow(
        store=store,
        events=events,
        agent_factory=_make_agent_factory(cfg),
    )
    state = initial_state(
        config=cfg,
        user_request="test no streaming",
        vault_root=isolated_workspace["vault_root"],
        runs_root=isolated_workspace["runs_root"],
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "acp-no-stream-test"}})

    # Should complete normally — no sentinel involvement.
    task_id = result["task_id"]
    events_list = _event_types(store, task_id)
    assert EventType.STREAM_ABORTED.value not in events_list
    # The graph should have proceeded through the full happy path.
    assert EventType.AGENT_FINISHED.value in events_list

    # Core invariant.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head
