"""v0.5.6 tests — SQLite durable event store.

Covers:
  - Schema initialization (idempotent)
  - Single event append + query
  - Batch append in a transaction
  - Query by task_id, type, time range
  - Count
  - Rebuild from JSONL
  - Duplicate event_id rejection
  - Context manager usage
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from acp.evidence.durable_store import DurableEventStore
from acp.events import EventWriter, GENESIS_HASH
from acp.models import Event, EventType


def _make_event(event_id: str, task_id: str, etype: EventType, payload: dict | None = None) -> Event:
    return Event(
        event_id=event_id,
        task_id=task_id,
        type=etype,
        payload=payload or {},
        prev_hash=GENESIS_HASH,
        hash="abc123",
    )


def test_durable_store_init_creates_schema(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    assert (tmp_path / "events.db").is_file()
    # Idempotent — second init doesn't error.
    db.init()
    db.close()


def test_durable_store_append_and_query(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    evt = _make_event("evt_000001", "task_001", EventType.TASK_CREATED, {"request": "test"})
    db.append(evt)
    results = db.query(task_id="task_001")
    assert len(results) == 1
    assert results[0].event_id == "evt_000001"
    assert results[0].type == EventType.TASK_CREATED
    assert results[0].payload == {"request": "test"}
    db.close()


def test_durable_store_batch_append(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    events = [
        _make_event("evt_000001", "task_001", EventType.TASK_CREATED),
        _make_event("evt_000002", "task_001", EventType.REPO_CHECKED, {"clean": True}),
        _make_event("evt_000003", "task_001", EventType.TASK_COMPLETED),
    ]
    db.append_batch(events)
    assert db.count() == 3
    assert db.count(task_id="task_001") == 3
    db.close()


def test_durable_store_query_by_type(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    db.append(_make_event("evt_000001", "task_001", EventType.TASK_CREATED))
    db.append(_make_event("evt_000002", "task_001", EventType.TASK_FAILED))
    db.append(_make_event("evt_000003", "task_002", EventType.TASK_CREATED))
    results = db.query(type=EventType.TASK_CREATED)
    assert len(results) == 2
    assert all(r.type == EventType.TASK_CREATED for r in results)
    db.close()


def test_durable_store_query_by_time_range(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    db.append(Event(
        event_id="evt_000001", task_id="t1", type=EventType.TASK_CREATED,
        timestamp="2026-06-24T10:00:00Z", prev_hash=GENESIS_HASH, hash="h1",
    ))
    db.append(Event(
        event_id="evt_000002", task_id="t1", type=EventType.TASK_COMPLETED,
        timestamp="2026-06-24T12:00:00Z", prev_hash=GENESIS_HASH, hash="h2",
    ))
    since_results = db.query(since="2026-06-24T11:00:00Z")
    assert len(since_results) == 1
    assert since_results[0].event_id == "evt_000002"
    until_results = db.query(until="2026-06-24T11:00:00Z")
    assert len(until_results) == 1
    assert until_results[0].event_id == "evt_000001"
    db.close()


def test_durable_store_rebuild_from_jsonl(tmp_path: Path):
    # Create a JSONL event log.
    w = EventWriter("task_001", tmp_path / "run")
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})

    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    count = db.rebuild_from_jsonl(tmp_path / "run" / "events.jsonl")
    assert count == 3
    assert db.count() == 3
    # Query back and verify.
    results = db.query(task_id="task_001")
    assert len(results) == 3
    assert results[0].type == EventType.TASK_CREATED
    assert results[2].type == EventType.TASK_COMPLETED
    db.close()


def test_durable_store_duplicate_event_id_raises(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    evt = _make_event("evt_000001", "task_001", EventType.TASK_CREATED)
    db.append(evt)
    with pytest.raises(sqlite3.IntegrityError):
        db.append(evt)
    db.close()


def test_durable_store_context_manager(tmp_path: Path):
    db_path = tmp_path / "events.db"
    with DurableEventStore(db_path) as db:
        db.append(_make_event("evt_000001", "task_001", EventType.TASK_CREATED))
        assert db.count() == 1
    # After exit, the connection is closed.
    assert db._conn is None


def test_durable_store_rebuild_replaces_existing(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    # Insert some old events.
    db.append(_make_event("evt_old", "task_old", EventType.TASK_CREATED))
    assert db.count() == 1

    # Rebuild from a JSONL file — should replace, not append.
    w = EventWriter("task_new", tmp_path / "run")
    w.write(EventType.TASK_CREATED, {"request": "new"})
    count = db.rebuild_from_jsonl(tmp_path / "run" / "events.jsonl")
    assert count == 1
    assert db.count() == 1
    # The old event is gone.
    assert db.query(task_id="task_old") == []
    assert len(db.query(task_id="task_new")) == 1
    db.close()
