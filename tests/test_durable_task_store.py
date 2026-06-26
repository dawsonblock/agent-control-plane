"""v0.5.6 tests — SQLite durable task store.

Covers:
  - Schema initialization (idempotent)
  - Save + load (insert and upsert)
  - Query by status, repo_name
  - Count
  - Rebuild from task.json files
  - Context manager usage
  - Load non-existent task returns None
"""

from __future__ import annotations

from pathlib import Path


from acp.evidence.durable_task_store import DurableTaskStore
from acp.models import Task, TaskStatus


def _make_task(task_id: str = "task_20260624_0001", status: TaskStatus = TaskStatus.PASSED) -> Task:
    return Task(
        task_id=task_id,
        repo_name="demo",
        repo_path=Path("/tmp/demo"),
        base_branch="main",
        task_branch=f"agent/{task_id}",
        worktree_path=Path(f"/tmp/runs/{task_id}/worktree"),
        user_request="test task",
        status=status,
    )


def test_durable_task_store_init_creates_schema(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    assert (tmp_path / "tasks.db").is_file()
    db.init()  # idempotent
    db.close()


def test_durable_task_store_save_and_load(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    task = _make_task()
    db.save(task)
    loaded = db.load("task_20260624_0001")
    assert loaded is not None
    assert loaded.task_id == "task_20260624_0001"
    assert loaded.status == TaskStatus.PASSED
    assert loaded.repo_name == "demo"
    db.close()


def test_durable_task_store_upsert(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    task = _make_task(status=TaskStatus.CREATED)
    db.save(task)
    # Update status and save again.
    task.status = TaskStatus.PASSED
    db.save(task)
    assert db.count() == 1  # no duplicate
    loaded = db.load("task_20260624_0001")
    assert loaded.status == TaskStatus.PASSED
    db.close()


def test_durable_task_store_query_by_status(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    db.save(_make_task("task_001", TaskStatus.PASSED))
    db.save(_make_task("task_002", TaskStatus.FAILED))
    db.save(_make_task("task_003", TaskStatus.PASSED))
    passed = db.query(status=TaskStatus.PASSED)
    assert len(passed) == 2
    failed = db.query(status="failed")
    assert len(failed) == 1
    db.close()


def test_durable_task_store_query_by_repo(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    db.save(_make_task("task_001"))
    task2 = _make_task("task_002")
    task2.repo_name = "other"
    db.save(task2)
    results = db.query(repo_name="demo")
    assert len(results) == 1
    assert results[0].task_id == "task_001"
    db.close()


def test_durable_task_store_count(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    db.save(_make_task("task_001", TaskStatus.PASSED))
    db.save(_make_task("task_002", TaskStatus.FAILED))
    assert db.count() == 2
    assert db.count(status=TaskStatus.PASSED) == 1
    assert db.count(status="failed") == 1
    db.close()


def test_durable_task_store_load_nonexistent_returns_none(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    assert db.load("nonexistent") is None
    db.close()


def test_durable_task_store_rebuild_from_jsonl(tmp_path: Path):
    # Create task.json files under a runs root.
    runs = tmp_path / "runs"
    for i, status in enumerate([TaskStatus.PASSED, TaskStatus.FAILED], 1):
        task = _make_task(f"task_20260624_{i:04d}", status)
        run_dir = runs / task.task_id
        run_dir.mkdir(parents=True)
        (run_dir / "task.json").write_text(task.model_dump_json(indent=2))

    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    count = db.rebuild_from_jsonl(runs)
    assert count == 2
    assert db.count() == 2
    assert db.count(status=TaskStatus.PASSED) == 1
    db.close()


def test_durable_task_store_context_manager(tmp_path: Path):
    db_path = tmp_path / "tasks.db"
    with DurableTaskStore(db_path) as db:
        db.save(_make_task())
        assert db.count() == 1
    assert db._conn is None
