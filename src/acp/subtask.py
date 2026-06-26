"""Sub-task spawning — agent-to-agent delegation (Phase 3.2).

When an agent encounters a task it can't handle alone (e.g., needs a
specialized agent, or the task is too large), it can request spawning
a sub-task by emitting a structured line in its stdout:

    ACP_SPAWN_SUBTASK: <sub-task request description>

ACP parses these lines from the agent's stdout, records them, and emits
``task.subtask_spawned`` events in the hash-chained event log. Each
sub-task is linked to its parent task via ``parent_task_id``.

Sub-tasks are not executed immediately — they are recorded as spawn
requests and can be launched later via ``acp run --parent <task_id>``.
This preserves the control plane's authority over what runs and when.

Security properties:
  - The agent cannot spawn sub-tasks directly — it can only request them.
  - ACP validates and records every spawn request in the signed event log.
  - Sub-task count is bounded by ``agent.max_subtasks`` (default: 5) to
    prevent unbounded spawning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# The structured line format the agent emits to request a sub-task.
# Example: "ACP_SPAWN_SUBTASK: Refactor the auth module to use OAuth2"
_SPAWN_RE = re.compile(
    r"^ACP_SPAWN_SUBTASK:\s*(.+)$",
    re.MULTILINE,
)


@dataclass
class SubTaskRequest:
    """A sub-task spawn request parsed from agent output.

    The parent task's event log records these as ``task.subtask_spawned``
    events with the ``parent_task_id`` and ``request`` fields.
    """

    request: str
    parent_task_id: str = ""


@dataclass
class SubTaskParseResult:
    """Result of parsing agent stdout for sub-task spawn requests.

    Contains the list of parsed requests and the remaining stdout with
    spawn lines stripped (so they don't appear in the agent's summary).
    """

    requests: list[SubTaskRequest] = field(default_factory=list)
    cleaned_stdout: str = ""


def parse_subtask_requests(
    stdout: str,
    parent_task_id: str = "",
    *,
    max_subtasks: int = 5,
) -> SubTaskParseResult:
    """Parse agent stdout for sub-task spawn requests.

    Scans for lines matching ``ACP_SPAWN_SUBTASK: <request>`` and returns
    a :class:`SubTaskParseResult` with the parsed requests and the
    stdout with spawn lines removed.

    Args:
        stdout: The agent's raw stdout text.
        parent_task_id: The task id of the parent task.
        max_subtasks: Maximum number of sub-task requests to parse
            (default: 5). Extra requests are silently dropped — the
            agent cannot overwhelm the control plane with spawn requests.

    Returns:
        A :class:`SubTaskParseResult` with the parsed requests and
        cleaned stdout.
    """
    requests: list[SubTaskRequest] = []
    cleaned_lines: list[str] = []

    for line in stdout.splitlines():
        m = _SPAWN_RE.match(line.strip())
        if m and len(requests) < max_subtasks:
            request = m.group(1).strip()
            if request:  # ignore empty requests
                requests.append(
                    SubTaskRequest(
                        request=request,
                        parent_task_id=parent_task_id,
                    )
                )
            # Spawn lines are stripped from the cleaned stdout regardless.
        else:
            cleaned_lines.append(line)

    return SubTaskParseResult(
        requests=requests,
        cleaned_stdout="\n".join(cleaned_lines),
    )


def emit_subtask_events(
    requests: list[SubTaskRequest],
    events_writer: Any,  # EventWriter
) -> int:
    """Emit task.subtask_spawned events for each sub-task request.

    Args:
        requests: The parsed sub-task requests.
        events_writer: The run's EventWriter (for the parent task).

    Returns:
        The number of events emitted.
    """
    from acp.models import EventType

    count = 0
    for i, req in enumerate(requests):
        events_writer.write(
            EventType.TASK_SUBTASK_SPAWNED,
            {
                "parent_task_id": req.parent_task_id,
                "subtask_index": i,
                "request": req.request,
            },
        )
        count += 1
    return count


# Type annotation helper — avoids importing EventWriter at module level.
from typing import Any  # noqa: E402
