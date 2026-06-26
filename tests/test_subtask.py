"""Tests for v0.7.0 (Phase 3.2) — sub-task spawning.

Tests the sub-task spawn request parsing and event emission:

  1. parse_subtask_requests — parsing ACP_SPAWN_SUBTASK lines from stdout
  2. emit_subtask_events — task.subtask_spawned event emission
  3. max_subtasks enforcement — bounding spawn requests
  4. Config — max_subtasks field in AgentSection
"""

from __future__ import annotations

from acp.config import AgentSection
from acp.events import EventWriter, verify_event_chain
from acp.models import EventType
from acp.subtask import (
    SubTaskRequest,
    parse_subtask_requests,
    emit_subtask_events,
)


# --------------------------------------------------------------------------- #
# 1. parse_subtask_requests
# --------------------------------------------------------------------------- #


def test_parse_single_subtask():
    """A single ACP_SPAWN_SUBTASK line is parsed correctly."""
    stdout = (
        "Starting work...\n"
        "ACP_SPAWN_SUBTASK: Refactor the auth module\n"
        "Done.\n"
    )
    result = parse_subtask_requests(stdout, parent_task_id="task_20260626_0001")
    assert len(result.requests) == 1
    assert result.requests[0].request == "Refactor the auth module"
    assert result.requests[0].parent_task_id == "task_20260626_0001"


def test_parse_multiple_subtasks():
    """Multiple ACP_SPAWN_SUBTASK lines are all parsed."""
    stdout = (
        "ACP_SPAWN_SUBTASK: Write tests for auth module\n"
        "ACP_SPAWN_SUBTASK: Update API documentation\n"
        "ACP_SPAWN_SUBTASK: Migrate database schema\n"
    )
    result = parse_subtask_requests(stdout, parent_task_id="task_001")
    assert len(result.requests) == 3
    assert result.requests[0].request == "Write tests for auth module"
    assert result.requests[1].request == "Update API documentation"
    assert result.requests[2].request == "Migrate database schema"


def test_parse_no_subtasks():
    """Stdout without spawn lines returns empty requests."""
    stdout = "Just normal agent output\nNo spawn requests here\n"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 0
    # cleaned_stdout preserves the content (splitlines/join may drop trailing \n).
    assert "Just normal agent output" in result.cleaned_stdout
    assert "No spawn requests here" in result.cleaned_stdout


def test_parse_strips_spawn_lines_from_stdout():
    """Spawn lines are removed from the cleaned stdout."""
    stdout = (
        "Line 1\n"
        "ACP_SPAWN_SUBTASK: Do something\n"
        "Line 3\n"
    )
    result = parse_subtask_requests(stdout)
    assert "ACP_SPAWN_SUBTASK" not in result.cleaned_stdout
    assert "Line 1" in result.cleaned_stdout
    assert "Line 3" in result.cleaned_stdout


def test_parse_empty_request_ignored():
    """ACP_SPAWN_SUBTASK: with empty request is ignored."""
    stdout = "ACP_SPAWN_SUBTASK:   \n"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 0


def test_parse_max_subtasks_enforced():
    """Only max_subtasks requests are parsed; extras are dropped."""
    lines = [f"ACP_SPAWN_SUBTASK: task {i}" for i in range(10)]
    stdout = "\n".join(lines)
    result = parse_subtask_requests(stdout, max_subtasks=3)
    assert len(result.requests) == 3
    assert result.requests[0].request == "task 0"
    assert result.requests[2].request == "task 2"


def test_parse_default_max_subtasks():
    """Default max_subtasks is 5."""
    lines = [f"ACP_SPAWN_SUBTASK: task {i}" for i in range(10)]
    stdout = "\n".join(lines)
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 5


# --------------------------------------------------------------------------- #
# 2. emit_subtask_events
# --------------------------------------------------------------------------- #


def test_emit_subtask_events(tmp_path):
    """task.subtask_spawned events are written to the event log."""
    run_dir = tmp_path / "task_20260626_0001"
    run_dir.mkdir()
    writer = EventWriter("task_20260626_0001", run_dir)

    requests = [
        SubTaskRequest(request="Write tests", parent_task_id="task_20260626_0001"),
        SubTaskRequest(request="Update docs", parent_task_id="task_20260626_0001"),
    ]
    count = emit_subtask_events(requests, writer)
    assert count == 2

    events = writer.read_all()
    spawned = [e for e in events if e.type == EventType.TASK_SUBTASK_SPAWNED]
    assert len(spawned) == 2
    assert spawned[0].payload["parent_task_id"] == "task_20260626_0001"
    assert spawned[0].payload["request"] == "Write tests"
    assert spawned[0].payload["subtask_index"] == 0
    assert spawned[1].payload["request"] == "Update docs"
    assert spawned[1].payload["subtask_index"] == 1


def test_emit_subtask_events_empty():
    """No requests → no events."""
    # Use a mock to avoid filesystem operations.
    from unittest.mock import MagicMock
    writer = MagicMock()
    count = emit_subtask_events([], writer)
    assert count == 0
    writer.write.assert_not_called()


def test_subtask_events_form_valid_hash_chain(tmp_path):
    """Sub-task events form a valid hash chain with other events."""
    run_dir = tmp_path / "task_20260626_0001"
    run_dir.mkdir()
    writer = EventWriter("task_20260626_0001", run_dir)

    # Write some events before and after the sub-task events.
    writer.write(EventType.TASK_CREATED, {"request": "parent task"})
    requests = [
        SubTaskRequest(request="child 1", parent_task_id="task_20260626_0001"),
        SubTaskRequest(request="child 2", parent_task_id="task_20260626_0001"),
    ]
    emit_subtask_events(requests, writer)
    writer.write(EventType.TASK_COMPLETED, {})

    events = writer.read_all()
    assert len(events) == 4
    assert verify_event_chain(events), "hash chain broken by sub-task events"


# --------------------------------------------------------------------------- #
# 4. Config — max_subtasks in AgentSection
# --------------------------------------------------------------------------- #


def test_agent_section_has_max_subtasks():
    """AgentSection has a max_subtasks field with default 5."""
    cfg = AgentSection()
    assert cfg.max_subtasks == 5


def test_agent_section_max_subtasks_custom():
    """AgentSection accepts custom max_subtasks values."""
    cfg = AgentSection(max_subtasks=10)
    assert cfg.max_subtasks == 10


def test_agent_section_max_subtasks_in_yaml(tmp_path):
    """Repo config loads max_subtasks from YAML."""
    import yaml
    config_file = tmp_path / "test.repo.yaml"
    config_file.write_text(yaml.dump({
        "repo": {"name": "test", "path": str(tmp_path)},
        "agent": {"max_subtasks": 3},
    }))
    from acp.config import load_repo_config
    cfg = load_repo_config(config_file)
    assert cfg.agent.max_subtasks == 3
