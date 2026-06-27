"""Edge-case tests for src/acp/subtask.py.

``subtask.py`` parses ``ACP_SPAWN_SUBTASK:`` lines from agent stdout into
:class:`SubTaskRequest` objects and emits ``task.subtask_spawned`` events.
Sub-tasks are *requests*, not immediate executions — the control plane
retains authority over what runs. These tests cover the edge cases around
that parsing/emission path: bounding, empty/whitespace descriptions,
invalid parent task ids, spawn-failure error handling, and the
``max_subtasks`` bound acting as the depth/count limit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from acp.events import EventWriter
from acp.models import EventType
from acp.subtask import (
    SubTaskParseResult,
    SubTaskRequest,
    emit_subtask_events,
    parse_subtask_requests,
)

# --------------------------------------------------------------------------- #
# max_subtasks — the bound on spawn requests (acts as the depth/count limit)
# --------------------------------------------------------------------------- #


def test_subtask_max_depth_enforced():
    """Only max_subtasks requests are parsed; extras are silently dropped."""
    lines = [f"ACP_SPAWN_SUBTASK: task {i}" for i in range(20)]
    stdout = "\n".join(lines)
    result = parse_subtask_requests(stdout, max_subtasks=4)
    assert len(result.requests) == 4
    assert result.requests[0].request == "task 0"
    assert result.requests[3].request == "task 3"


def test_subtask_max_depth_zero_blocks_all():
    """max_subtasks=0 means no spawn requests are accepted.

    Note: with max_subtasks=0 the spawn line no longer matches the
    "parse" branch (the count check fails), so it is kept in the
    cleaned stdout as an ordinary line. The important guarantee is
    that no request is recorded.
    """
    stdout = "ACP_SPAWN_SUBTASK: do something\n"
    result = parse_subtask_requests(stdout, max_subtasks=0)
    assert len(result.requests) == 0


def test_subtask_default_max_is_five():
    """The default max_subtasks bound is 5."""
    lines = [f"ACP_SPAWN_SUBTASK: task {i}" for i in range(12)]
    result = parse_subtask_requests("\n".join(lines))
    assert len(result.requests) == 5


# --------------------------------------------------------------------------- #
# Empty / whitespace descriptions
# --------------------------------------------------------------------------- #


def test_subtask_empty_description_ignored():
    """ACP_SPAWN_SUBTASK: with an empty request is ignored."""
    stdout = "ACP_SPAWN_SUBTASK:\n"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 0


def test_subtask_whitespace_description_ignored():
    """ACP_SPAWN_SUBTASK: with only whitespace is ignored."""
    stdout = "ACP_SPAWN_SUBTASK:   \t  \n"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 0


def test_subtask_empty_description_kept_as_normal_line():
    """An empty spawn line (no content after the colon) doesn't match the
    regex after stripping, so it is treated as an ordinary stdout line
    and kept in cleaned_stdout (and produces no request).
    """
    stdout = "keep me\nACP_SPAWN_SUBTASK:   \nalso keep me\n"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 0
    assert "keep me" in result.cleaned_stdout
    assert "also keep me" in result.cleaned_stdout


# --------------------------------------------------------------------------- #
# Invalid / empty parent task ids
# --------------------------------------------------------------------------- #


def test_subtask_invalid_task_id_defaults_empty():
    """An empty parent_task_id is accepted (defaults to '')."""
    stdout = "ACP_SPAWN_SUBTASK: do work\n"
    result = parse_subtask_requests(stdout, parent_task_id="")
    assert len(result.requests) == 1
    assert result.requests[0].parent_task_id == ""


def test_subtask_parent_task_id_propagated():
    """The parent_task_id is attached to each parsed request."""
    stdout = "ACP_SPAWN_SUBTASK: a\nACP_SPAWN_SUBTASK: b\n"
    result = parse_subtask_requests(stdout, parent_task_id="task_20260626_0042")
    assert all(r.parent_task_id == "task_20260626_0042" for r in result.requests)


def test_subtask_request_dataclass_defaults():
    """SubTaskRequest defaults request to required, parent_task_id to ''."""
    req = SubTaskRequest(request="hello")
    assert req.request == "hello"
    assert req.parent_task_id == ""


# --------------------------------------------------------------------------- #
# Spawn-failure error handling (emit_subtask_events)
# --------------------------------------------------------------------------- #


def test_subtask_spawn_failure_raises():
    """If the event writer raises during emission, the error propagates.

    The control plane must not silently swallow a write failure — a
    broken event log is a fatal integrity problem.
    """
    writer = MagicMock()
    writer.write.side_effect = OSError("disk full")
    requests = [SubTaskRequest(request="boom", parent_task_id="task_fail")]
    with pytest.raises(OSError, match="disk full"):
        emit_subtask_events(requests, writer)


def test_subtask_emit_empty_requests_no_calls():
    """No requests → writer.write is never called and count is 0."""
    writer = MagicMock()
    count = emit_subtask_events([], writer)
    assert count == 0
    writer.write.assert_not_called()


def test_subtask_emit_count_matches_requests(tmp_path):
    """emit_subtask_events returns the number of events written."""
    run_dir = tmp_path / "task_ok"
    run_dir.mkdir()
    writer = EventWriter("task_ok", run_dir)
    requests = [SubTaskRequest(request=f"req {i}", parent_task_id="task_ok") for i in range(3)]
    count = emit_subtask_events(requests, writer)
    assert count == 3
    events = writer.read_all()
    spawned = [e for e in events if e.type == EventType.TASK_SUBTASK_SPAWNED]
    assert len(spawned) == 3
    assert [e.payload["subtask_index"] for e in spawned] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# Parsing shape / cyclic-style repeated lines
# --------------------------------------------------------------------------- #


def test_subtask_repeated_identical_requests_allowed():
    """Identical repeated spawn lines are each parsed (no dedup)."""
    stdout = "ACP_SPAWN_SUBTASK: same thing\nACP_SPAWN_SUBTASK: same thing\n"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 2
    assert result.requests[0].request == "same thing"
    assert result.requests[1].request == "same thing"


def test_subtask_parse_result_defaults():
    """SubTaskParseResult defaults to empty requests and stdout."""
    result = SubTaskParseResult()
    assert result.requests == []
    assert result.cleaned_stdout == ""


def test_subtask_no_spawn_lines_preserves_stdout():
    """Stdout without spawn lines is returned (modulo splitlines/join)."""
    stdout = "line one\nline two\nline three"
    result = parse_subtask_requests(stdout)
    assert len(result.requests) == 0
    assert "line one" in result.cleaned_stdout
    assert "line three" in result.cleaned_stdout
