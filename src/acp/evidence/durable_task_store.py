"""SQLite durable task store — transactional task persistence.

The JSONL event log is the source of truth for *what happened*. The task
store is the source of truth for *what state a task is in*. This module
provides a SQLite-backed task store for operators who need:

  * **Transactional durability**: task state changes are atomic.
  * **Queryability**: list tasks by status, find tasks for a repo, etc.
  * **Cross-run indexing**: a single database holds all tasks.

Like the durable event store, this is **additive** to the JSON `task.json`
file (which remains the canonical per-run source). The SQLite store is a
queryable index that can be rebuilt from the `task.json` files.

Usage::

    store = DurableTaskStore(db_path)
    store.init()
    store.save(task)
    tasks = store.query(status="failed")

Schema:

    CREATE TABLE tasks (
        task_id         TEXT PRIMARY KEY,
        repo_name       TEXT NOT NULL,
        repo_path       TEXT NOT NULL,
        base_branch     TEXT NOT NULL,
        base_commit_sha TEXT DEFAULT '',
        task_branch     TEXT NOT NULL,
        worktree_path   TEXT NOT NULL,
        user_request    TEXT NOT NULL,
        status          TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX idx_tasks_status ON tasks(status);
    CREATE INDEX idx_tasks_repo ON tasks(repo_name);
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from acp.models import Task, TaskStatus


class DurableTaskStore:
    """SQLite-backed task store — transactional, queryable, crash-safe.

    Uses WAL mode for concurrent read access and fsync-on-commit for
    durability. Additive to the JSON task.json files (which remain the
    canonical per-run source).
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """Initialize the database schema. Idempotent."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id         TEXT PRIMARY KEY,
                repo_name       TEXT NOT NULL,
                repo_path       TEXT NOT NULL,
                base_branch     TEXT NOT NULL,
                base_commit_sha TEXT DEFAULT '',
                task_branch     TEXT NOT NULL,
                worktree_path   TEXT NOT NULL,
                user_request    TEXT NOT NULL,
                status          TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_repo ON tasks(repo_name)")

    def save(self, task: Task) -> None:
        """Insert or update a task. Idempotent (upsert)."""
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        self._conn.execute(
            "INSERT INTO tasks (task_id, repo_name, repo_path, base_branch, base_commit_sha, "
            "task_branch, worktree_path, user_request, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "base_commit_sha=excluded.base_commit_sha, "
            "status=excluded.status, updated_at=excluded.updated_at",
            (
                task.task_id,
                task.repo_name,
                str(task.repo_path),
                task.base_branch,
                task.base_commit_sha,
                task.task_branch,
                str(task.worktree_path),
                task.user_request,
                task.status.value,
                task.created_at,
                task.updated_at,
            ),
        )

    def load(self, task_id: str) -> Task | None:
        """Load a task by id. Returns None if not found."""
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        row = self._conn.execute(
            "SELECT task_id, repo_name, repo_path, base_branch, base_commit_sha, "
            "task_branch, worktree_path, user_request, status, created_at, updated_at "
            "FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_task(row)

    def query(
        self,
        *,
        status: str | TaskStatus | None = None,
        repo_name: str | None = None,
        limit: int = 1000,
    ) -> list[Task]:
        """Query tasks by status and/or repo. Returns in creation order."""
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            status_str = status.value if isinstance(status, TaskStatus) else status
            clauses.append("status = ?")
            params.append(status_str)
        if repo_name is not None:
            clauses.append("repo_name = ?")
            params.append(repo_name)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT task_id, repo_name, repo_path, base_branch, base_commit_sha, "
            f"task_branch, worktree_path, user_request, status, created_at, updated_at "
            f"FROM tasks{where} ORDER BY created_at ASC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def count(self, *, status: str | TaskStatus | None = None) -> int:
        """Count tasks, optionally filtered by status."""
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        if status is not None:
            status_str = status.value if isinstance(status, TaskStatus) else status
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ?", (status_str,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return row[0] if row else 0

    def rebuild_from_jsonl(self, runs_root: Path | str) -> int:
        """Rebuild the SQLite store from task.json files under runs_root.

        Drops all existing tasks and re-imports from the task.json files.
        Returns the number of tasks imported.
        """
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        self._conn.execute("DELETE FROM tasks")
        runs_root = Path(runs_root)
        if not runs_root.is_dir():
            return 0
        count = 0
        for task_json in sorted(runs_root.rglob("task.json")):
            try:
                task = Task.model_validate_json(task_json.read_text())
                self.save(task)
                count += 1
            except Exception:  # noqa: BLE001
                pass  # skip malformed task.json
        return count

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> DurableTaskStore:
        self.init()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _row_to_task(row: tuple) -> Task:
    """Convert a SQLite row to a Task model."""
    return Task(
        task_id=row[0],
        repo_name=row[1],
        repo_path=Path(row[2]),
        base_branch=row[3],
        base_commit_sha=row[4],
        task_branch=row[5],
        worktree_path=Path(row[6]),
        user_request=row[7],
        status=TaskStatus(row[8]),
        created_at=row[9],
        updated_at=row[10],
    )
