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

from acp.events import GENESIS_HASH, EventWriter
from acp.evidence.durable_store import DurableEventStore
from acp.models import Event, EventType


def _make_event(
    event_id: str, task_id: str, etype: EventType, payload: dict | None = None
) -> Event:
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
    db.append(
        Event(
            event_id="evt_000001",
            task_id="t1",
            type=EventType.TASK_CREATED,
            timestamp="2026-06-24T10:00:00Z",
            prev_hash=GENESIS_HASH,
            hash="h1",
        )
    )
    db.append(
        Event(
            event_id="evt_000002",
            task_id="t1",
            type=EventType.TASK_COMPLETED,
            timestamp="2026-06-24T12:00:00Z",
            prev_hash=GENESIS_HASH,
            hash="h2",
        )
    )
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


# v0.9.0 (Step 1) — rebuild_jsonl_from_store + get_event_count


def test_durable_store_get_event_count(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    db.append(_make_event("evt_000001", "task_A", EventType.TASK_CREATED))
    db.append(_make_event("evt_000002", "task_A", EventType.TASK_COMPLETED))
    db.append(_make_event("evt_000001", "task_B", EventType.TASK_CREATED))
    assert db.get_event_count("task_A") == 2
    assert db.get_event_count("task_B") == 1
    assert db.get_event_count("task_C") == 0
    db.close()


def test_durable_store_rebuild_jsonl_from_store(tmp_path: Path):
    """Rebuild events.jsonl from the SQLite store (inverse of rebuild_from_jsonl)."""
    # 1. Write events to JSONL via EventWriter.
    run_dir = tmp_path / "run"
    w = EventWriter("task_001", run_dir)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})

    # 2. Import into SQLite.
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    db.rebuild_from_jsonl(run_dir / "events.jsonl")
    assert db.get_event_count("task_001") == 3

    # 3. Delete the JSONL file (simulate crash/corruption).
    jsonl_path = run_dir / "events.jsonl"
    jsonl_path.unlink()
    assert not jsonl_path.exists()

    # 4. Rebuild JSONL from SQLite.
    rebuilt = db.rebuild_jsonl_from_store("task_001", jsonl_path)
    assert rebuilt == 3
    assert jsonl_path.is_file()

    # 5. Verify the rebuilt JSONL has the same events in order.
    w2 = EventWriter("task_001", run_dir)
    events = w2.read_all()
    assert len(events) == 3
    assert events[0].type == EventType.TASK_CREATED
    assert events[1].type == EventType.REPO_CHECKED
    assert events[2].type == EventType.TASK_COMPLETED
    # Hash chain should be intact.
    from acp.events import verify_event_chain

    assert verify_event_chain(events)
    db.close()


def test_durable_store_rebuild_jsonl_from_store_empty(tmp_path: Path):
    """Rebuild with no events in SQLite returns 0 and does not create a file."""
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    jsonl_path = tmp_path / "run" / "events.jsonl"
    rebuilt = db.rebuild_jsonl_from_store("nonexistent_task", jsonl_path)
    assert rebuilt == 0
    assert not jsonl_path.exists()
    db.close()


async def test_create_task_rebuilds_jsonl_from_sqlite(tmp_path: Path):
    """v0.9.0 (Step 1): create_task rebuilds events.jsonl from SQLite on startup.

    Simulates crash recovery: a task has events in the SQLite durable store
    but no events.jsonl (e.g., the JSONL was lost in a crash). When create_task
    runs with the durable store wired, it should rebuild the JSONL from SQLite
    before writing the task.created event.
    """
    import subprocess

    from acp.config import (
        AgentSection,
        CommandsSection,
        ExecutorSection,
        RepoConfig,
        RepoSection,
        ReviewSection,
    )
    from acp.graph.nodes import NodeContext, create_task
    from acp.store import TaskStore

    # 1. Set up a disposable git repo.
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "test"], check=True)
    (repo_path / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "init"], check=True)

    # 2. Seed the durable store with pre-existing events for a task (simulating
    #    a prior crashed run). We write events directly to SQLite without
    #    creating a run_dir — the run_dir will be created by store.create().
    runs_root = tmp_path / "runs"
    task_id = "task_20260624_0001"
    db = DurableEventStore(tmp_path / "events.db")
    db.init()

    # Build events with a proper hash chain (as if from a prior run).
    w = EventWriter(task_id, tmp_path / "_seed")  # temp dir, not the real run_dir
    evt1 = w.build_event(EventType.TASK_CREATED, {"request": "prior request"})
    w.append_event(evt1)
    evt2 = w.build_event(EventType.REPO_CHECKED, {"clean": True})
    w.append_event(evt2)
    db.append_batch([evt1, evt2])
    assert db.get_event_count(task_id) == 2

    # 3. Set up NodeContext with the durable store and call create_task.
    #    No run_dir exists yet — store.create() will create it, then the
    #    rebuild-from-SQLite path will write events.jsonl from SQLite.
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(),
        commands=CommandsSection(lint='echo "lint ok"', test='echo "tests passed"'),
        review=ReviewSection(require_human_approval=False),
        executor=ExecutorSection(backend="worktree", danger_allow_host_shell=True),
    )
    ctx = NodeContext(
        store=store,
        events=events,
        durable_event_store=db,
    )
    state = {
        "config": cfg,
        "user_request": "new request",
        "preallocated_task_id": task_id,
        "recursion_depth": 0,
    }

    await create_task(state, ctx)

    # 4. Verify the JSONL was rebuilt from SQLite.
    jsonl_path = store.run_dir(task_id) / "events.jsonl"
    assert jsonl_path.is_file(), "events.jsonl should have been rebuilt from SQLite"

    # The EventWriter should have picked up the rebuilt events (count >= 2)
    # and then appended the new task.created event (count >= 3).
    all_events = events.read_all()
    assert len(all_events) >= 3, f"expected >= 3 events, got {len(all_events)}"

    # The first two events are the rebuilt ones; the third is the new task.created.
    assert all_events[0].type == EventType.TASK_CREATED
    assert all_events[1].type == EventType.REPO_CHECKED
    assert all_events[2].type == EventType.TASK_CREATED

    # The hash chain should be intact across the rebuild + append boundary.
    from acp.events import verify_event_chain

    assert verify_event_chain(all_events), "hash chain broken after rebuild + append"

    db.close()
