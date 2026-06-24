"""Event log — the source of truth.

Every meaningful action in a run appends one event to
``data/runs/<task_id>/events.jsonl``. The report is the human-readable
projection of this log; Graphiti memory is derived from it. If it's not
in this file, it didn't happen.

Events form a hash chain (v0.5.5): each event's ``hash`` is sha256 of
``prev_hash + event_id + task_id + type + timestamp + payload``. The first
event's ``prev_hash`` is the literal string ``"GENESIS"``. This makes the
log tamper-evident — any removal, reordering, or modification breaks the
chain and is detectable by :func:`verify_event_chain`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from acp.models import Event, EventType, next_event_id

GENESIS_HASH = "GENESIS"


def _compute_event_hash(
    *,
    prev_hash: str,
    event_id: str,
    task_id: str,
    event_type: str,
    timestamp: str,
    payload: dict[str, Any],
) -> str:
    """sha256 of the canonical-JSON encoding of the event's chain fields."""
    content = json.dumps(
        {
            "prev_hash": prev_hash,
            "event_id": event_id,
            "task_id": task_id,
            "type": event_type,
            "timestamp": timestamp,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode()).hexdigest()


def verify_event_chain(events: list[Event]) -> bool:
    """Verify the hash chain of an event list.

    Returns ``True`` iff every event's ``prev_hash`` matches the preceding
    event's ``hash`` and every event's ``hash`` matches the recomputed
    value. The first event must have ``prev_hash == "GENESIS"``.
    """
    prev = GENESIS_HASH
    for evt in events:
        if evt.prev_hash != prev:
            return False
        expected = _compute_event_hash(
            prev_hash=evt.prev_hash,
            event_id=evt.event_id,
            task_id=evt.task_id,
            event_type=evt.type.value,
            timestamp=evt.timestamp,
            payload=evt.payload,
        )
        if evt.hash != expected:
            return False
        prev = evt.hash
    return True


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
        self._prev_hash = GENESIS_HASH
        if self.path.exists():
            # Resume counting + hash chain after a restart / repair attempt.
            for line in self.path.open():
                if not line.strip():
                    continue
                self._count += 1
                try:
                    evt = Event.model_validate_json(line)
                    self._prev_hash = evt.hash or GENESIS_HASH
                except Exception:  # noqa: BLE001
                    pass  # malformed line — keep the last good hash

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
        self._count = 0
        self._prev_hash = GENESIS_HASH
        if self.path.exists():
            for line in self.path.open():
                if not line.strip():
                    continue
                self._count += 1
                try:
                    evt = Event.model_validate_json(line)
                    self._prev_hash = evt.hash or GENESIS_HASH
                except Exception:  # noqa: BLE001
                    pass

    def write(
        self, type: EventType, payload: dict[str, Any] | None = None
    ) -> Event:
        """Append one event and return the constructed Event object."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        event_id = next_event_id(self._count)
        timestamp = _utcnow_iso()
        payload = payload or {}
        hash_value = _compute_event_hash(
            prev_hash=self._prev_hash,
            event_id=event_id,
            task_id=self.task_id,
            event_type=type.value,
            timestamp=timestamp,
            payload=payload,
        )
        event = Event(
            event_id=event_id,
            task_id=self.task_id,
            type=type,
            timestamp=timestamp,
            payload=payload,
            prev_hash=self._prev_hash,
            hash=hash_value,
        )
        with self.path.open("a") as f:
            f.write(event.model_dump_json() + "\n")
        self._count += 1
        self._prev_hash = hash_value
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

    @property
    def last_hash(self) -> str:
        """The hash of the most recently written event (or GENESIS)."""
        return self._prev_hash


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with a trailing Z."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
