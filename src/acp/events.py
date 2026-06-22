"""Event log — the source of truth.

Every meaningful action in a run appends one event to
``data/runs/<task_id>/events.jsonl``. The report is the human-readable
projection of this log; Graphiti memory is derived from it. If it's not
in this file, it didn't happen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from acp.models import Event, EventType, next_event_id


class EventWriter:
    """Append-only writer for a task's event log.

    One writer per task. Thread-unsafe by design: a task runs linearly (M1)
    or under a single graph invocation (M3).
    """

    def __init__(self, task_id: str, run_dir: Path) -> None:
        self.task_id = task_id
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "events.jsonl"
        self._count = 0
        if self.path.exists():
            # Resume counting after a restart / repair attempt.
            self._count = sum(1 for _ in self.path.open())

    def relocate(self, task_id: str, run_dir: Path) -> None:
        """Repoint this writer at a task's real run dir.

        Used by the LangGraph entry node: the writer is constructed before
        the task id is known (the id is minted inside ``create_task``), so
        ``create_task`` calls ``relocate`` once it has the real id. Any
        events written before relocation are re-pointed in memory; in
        practice nothing is written before ``create_task`` runs.
        """
        self.task_id = task_id
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "events.jsonl"
        if self.path.exists():
            self._count = sum(1 for _ in self.path.open())

    def write(
        self, type: EventType, payload: dict[str, Any] | None = None
    ) -> Event:
        """Append one event and return the constructed Event object."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        event = Event(
            event_id=next_event_id(self._count),
            task_id=self.task_id,
            type=type,
            payload=payload or {},
        )
        with self.path.open("a") as f:
            f.write(event.model_dump_json() + "\n")
        self._count += 1
        return event

    def read_all(self) -> list[Event]:
        """Read every event in log order. Used by the report writer."""
        if not self.path.exists():
            return []
        return [
            Event.model_validate_json(line)
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]

    @property
    def count(self) -> int:
        return self._count
