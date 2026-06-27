"""Unit tests for the event system (EventWriter, Event model, next_event_id)."""

from __future__ import annotations

import json
from pathlib import Path

from acp.events import EventWriter
from acp.models import EventType, next_event_id


def test_next_event_id_starts_at_one() -> None:
    assert next_event_id(0) == "evt_000001"


def test_next_event_id_increments() -> None:
    assert next_event_id(5) == "evt_000006"
    assert next_event_id(99) == "evt_000100"
    assert next_event_id(999_999) == "evt_1000000"


def test_event_writer_creates_file(tmp_path: Path) -> None:
    writer = EventWriter("task_001", tmp_path / "runs" / "task_001")
    ev = writer.write(EventType.TASK_CREATED, {"request": "hello"})
    assert writer.path.exists()
    assert ev.task_id == "task_001"
    assert ev.type == EventType.TASK_CREATED
    assert ev.payload == {"request": "hello"}
    assert ev.event_id == "evt_000001"


def test_event_writer_appends_multiple(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "task_002"
    writer = EventWriter("task_002", run_dir)
    writer.write(EventType.REPO_CHECKED, {"clean": True})
    writer.write(EventType.WORKTREE_CREATED, {"branch": "agent/task_002"})
    lines = writer.path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == EventType.REPO_CHECKED.value
    assert json.loads(lines[1])["type"] == EventType.WORKTREE_CREATED.value


def test_event_writer_read_all(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "task_003"
    writer = EventWriter("task_003", run_dir)
    writer.write(EventType.TASK_CREATED)
    writer.write(EventType.AGENT_FINISHED)
    events = writer.read_all()
    assert len(events) == 2
    assert events[0].type == EventType.TASK_CREATED
    assert events[1].type == EventType.AGENT_FINISHED


def test_event_writer_relocate(tmp_path: Path) -> None:
    """relocate() repoints the writer at a new task id and run dir."""
    run_a = tmp_path / "runs" / "task_a"
    run_b = tmp_path / "runs" / "task_b"
    writer = EventWriter("task_a", run_a)
    writer.write(EventType.TASK_CREATED)

    writer.relocate("task_b", run_b)
    writer.write(EventType.REPO_CHECKED)

    # task_a log has 1 event, task_b log has 1 event.
    assert len(writer.read_all()) == 1  # writer points at task_b
    assert len([l for l in (run_a / "events.jsonl").read_text().splitlines() if l.strip()]) == 1
    assert len([l for l in (run_b / "events.jsonl").read_text().splitlines() if l.strip()]) == 1


def test_event_writer_count(tmp_path: Path) -> None:
    writer = EventWriter("task_004", tmp_path / "runs" / "task_004")
    assert writer.count == 0
    writer.write(EventType.TASK_CREATED)
    assert writer.count == 1
    writer.write(EventType.REPO_CHECKED)
    assert writer.count == 2
