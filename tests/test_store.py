"""Unit tests for TaskStore — task id generation, create, save, load."""

from __future__ import annotations

from pathlib import Path

from acp.models import TaskStatus
from acp.store import TaskStore


def test_next_task_id_starts_at_0001(tmp_path: Path) -> None:
    store = TaskStore(runs_root=tmp_path / "runs")
    tid = store.next_task_id()
    assert tid.endswith("_0001")


def test_next_task_id_increments(tmp_path: Path) -> None:
    store = TaskStore(runs_root=tmp_path / "runs")
    t1 = store.next_task_id()
    # Create a run dir to simulate an existing task.
    (store.run_dir(t1)).mkdir(parents=True)
    t2 = store.next_task_id()
    assert t2 > t1  # lexicographic order since zero-padded


def test_create_initializes_run_dir(tmp_path: Path) -> None:
    store = TaskStore(runs_root=tmp_path / "runs")
    task = store.create(
        task_id="task_20260622_0001",
        repo_name="demo",
        repo_path=tmp_path / "repo",
        base_branch="main",
        user_request="test task",
    )
    assert task.status == TaskStatus.CREATED
    assert task.task_branch == "agent/task_20260622_0001"
    assert store.run_dir("task_20260622_0001").exists()
    assert store.task_json_path("task_20260622_0001").exists()


def test_create_raises_on_duplicate(tmp_path: Path) -> None:
    store = TaskStore(runs_root=tmp_path / "runs")
    store.create(
        task_id="task_20260622_0001",
        repo_name="demo",
        repo_path=tmp_path / "repo",
        base_branch="main",
        user_request="first",
    )
    import pytest

    with pytest.raises(FileExistsError):
        store.create(
            task_id="task_20260622_0001",
            repo_name="demo",
            repo_path=tmp_path / "repo",
            base_branch="main",
            user_request="second",
        )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = TaskStore(runs_root=tmp_path / "runs")
    task = store.create(
        task_id="task_20260622_0001",
        repo_name="demo",
        repo_path=tmp_path / "repo",
        base_branch="main",
        user_request="round trip",
    )
    task.status = TaskStatus.PASSED
    task.touch()
    store.save(task)

    loaded = store.load("task_20260622_0001")
    assert loaded.task_id == "task_20260622_0001"
    assert loaded.status == TaskStatus.PASSED
    assert loaded.user_request == "round trip"


def test_layout_helpers(tmp_path: Path) -> None:
    store = TaskStore(runs_root=tmp_path / "runs")
    assert str(store.run_dir("t1")).endswith("runs/t1")
    assert str(store.artifacts_dir("t1")).endswith("runs/t1/artifacts")
    assert str(store.worktree_path("t1")).endswith("runs/t1/worktree")
    assert str(store.events_path("t1")).endswith("runs/t1/events.jsonl")
    assert str(store.task_json_path("t1")).endswith("runs/t1/task.json")
