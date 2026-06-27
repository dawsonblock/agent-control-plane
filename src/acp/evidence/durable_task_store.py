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

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acp.models import Task, TaskStatus


def _now_iso() -> str:
    """Current UTC time in ISO format."""
    return datetime.now(UTC).isoformat()


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
        """Initialize the database schema. Idempotent.

        Uses the forward-rolling migration engine (``acp.evidence.migrations``)
        to apply schema updates via a per-store ``schema_versions`` table.
        This avoids the O(N) drop-and-rebuild strategy as the database grows.
        """
        from acp.evidence.migrations import TASK_STORE_MIGRATIONS, run_migrations

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")

        # Run forward-rolling migrations via the schema_versions table.
        run_migrations(self._conn, TASK_STORE_MIGRATIONS, store_name="task_store")

    def save(self, task: Task) -> None:
        """Insert or update a task. Idempotent (upsert)."""
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        self._conn.execute(
            "INSERT INTO tasks (task_id, repo_name, repo_path, base_branch, base_commit_sha, "
            "task_branch, worktree_path, user_request, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "repo_name=excluded.repo_name, "
            "repo_path=excluded.repo_path, "
            "base_branch=excluded.base_branch, "
            "base_commit_sha=excluded.base_commit_sha, "
            "task_branch=excluded.task_branch, "
            "worktree_path=excluded.worktree_path, "
            "user_request=excluded.user_request, "
            "status=excluded.status, "
            "created_at=excluded.created_at, "
            "updated_at=excluded.updated_at",
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

    def find_orphaned_tasks(self) -> list[Task]:
        """Find tasks in non-terminal states (created, executing, reviewing).

        These are tasks that were interrupted by a server crash or SIGKILL
        and never reached a terminal state. They should be recovered on
        the next server startup.
        """
        orphaned_statuses = (
            TaskStatus.CREATED.value,
            TaskStatus.EXECUTING.value,
            TaskStatus.REVIEWING.value,
        )
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")
        placeholders = ",".join("?" * len(orphaned_statuses))
        rows = self._conn.execute(
            f"SELECT task_id, repo_name, repo_path, base_branch, base_commit_sha, "
            f"task_branch, worktree_path, user_request, status, created_at, updated_at "
            f"FROM tasks WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            orphaned_statuses,
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def mark_orphaned(self, task_id: str, reason: str = "server restart") -> None:
        """Mark an orphaned task as failed with an orphan reason.

        Updates the SQLite store. Use ``recover_orphaned_tasks`` for full
        recovery (task.json + worktree cleanup).
        """
        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")

        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, orphan_reason = ? WHERE task_id = ?",
            (TaskStatus.FAILED.value, _now_iso(), reason, task_id),
        )

    def recover_orphaned_tasks(
        self,
        runs_root: Path | str,
        on_recovered: Any = None,
    ) -> list[str]:
        """Recover orphaned tasks found in the SQLite store.

        Marks each orphaned task as FAILED with an "orphaned by server
        restart" reason. Also updates the task.json file and attempts to
        clean up the git worktree if it still exists.

        Returns a list of recovered task IDs. Calls ``on_recovered(task_id)``
        for each recovered task if the callback is provided.
        """
        from acp.gitops.worktrees import remove_worktree
        from acp.store import TaskStore

        orphans = self.find_orphaned_tasks()
        recovered: list[str] = []

        store = TaskStore(runs_root=Path(runs_root))

        for task in orphans:
            try:
                # Update task.json.
                task.status = TaskStatus.FAILED
                task.touch()
                store.save(task)

                # Update SQLite with orphan_reason.
                self.mark_orphaned(task.task_id, reason="orphaned by server restart")

                # Clean up worktree if it exists.
                wt_path = task.worktree_path
                if wt_path and Path(wt_path).is_dir():
                    try:
                        remove_worktree(task.repo_path, Path(wt_path))
                    except Exception:  # noqa: BLE001
                        pass  # best-effort cleanup

                recovered.append(task.task_id)
                if on_recovered is not None:
                    on_recovered(task.task_id)
            except Exception:  # noqa: BLE001
                pass  # best-effort — continue recovering other tasks

        return recovered

    def check_integrity(
        self,
        runs_root: Path | str,
        *,
        on_breach: Any = None,
    ) -> list[dict[str, str]]:
        """Check for status mismatches between task.json and SQLite.

        For every task present in both stores, compares the ``status``
        field. If they disagree, records a breach entry and calls
        ``on_breach(task_id, json_status, sqlite_status)`` if provided.

        Returns a list of breach dicts:
        ``{"task_id": ..., "json_status": ..., "sqlite_status": ...}``

        This is the v0.7.1 integrity check — fail-closed: when breaches
        are detected, the caller should emit a ``store.integrity_breach``
        event and refuse to proceed with the affected tasks.
        """
        import logging

        logger = logging.getLogger(__name__)

        runs_root = Path(runs_root)
        breaches: list[dict[str, str]] = []

        if self._conn is None:
            raise RuntimeError("DurableTaskStore not initialized — call .init() first")

        # Get all tasks from SQLite.
        rows = self._conn.execute(
            "SELECT task_id, status FROM tasks ORDER BY created_at ASC",
        ).fetchall()

        for row in rows:
            task_id = row[0]
            sqlite_status = row[1]

            # Find the corresponding task.json.
            task_json_path = runs_root / task_id / "task.json"
            if not task_json_path.is_file():
                # task.json missing but SQLite has it — that's a breach.
                breaches.append(
                    {
                        "task_id": task_id,
                        "json_status": "(missing)",
                        "sqlite_status": sqlite_status,
                    }
                )
                logger.warning(
                    "integrity breach: task %s — task.json missing, SQLite status=%s",
                    task_id,
                    sqlite_status,
                )
                if on_breach is not None:
                    on_breach(task_id, "(missing)", sqlite_status)
                continue

            try:
                task = Task.model_validate_json(task_json_path.read_text())
                json_status = task.status.value
            except Exception as exc:  # noqa: BLE001
                breaches.append(
                    {
                        "task_id": task_id,
                        "json_status": f"(parse error: {exc})",
                        "sqlite_status": sqlite_status,
                    }
                )
                logger.warning(
                    "integrity breach: task %s — task.json parse error: %s, SQLite status=%s",
                    task_id,
                    exc,
                    sqlite_status,
                )
                if on_breach is not None:
                    on_breach(task_id, "(parse error)", sqlite_status)
                continue

            if json_status != sqlite_status:
                breaches.append(
                    {
                        "task_id": task_id,
                        "json_status": json_status,
                        "sqlite_status": sqlite_status,
                    }
                )
                logger.warning(
                    "integrity breach: task %s — task.json status=%s, SQLite status=%s",
                    task_id,
                    json_status,
                    sqlite_status,
                )
                if on_breach is not None:
                    on_breach(task_id, json_status, sqlite_status)

        # Reverse check: task.json files with no corresponding SQLite entry.
        sqlite_ids = {row[0] for row in rows}
        if runs_root.is_dir():
            for task_json in sorted(runs_root.rglob("task.json")):
                task_id = task_json.parent.name
                if task_id in sqlite_ids:
                    continue
                try:
                    task = Task.model_validate_json(task_json.read_text())
                    json_status = task.status.value
                except Exception as exc:  # noqa: BLE001
                    json_status = f"(parse error: {exc})"
                breaches.append(
                    {
                        "task_id": task_id,
                        "json_status": json_status,
                        "sqlite_status": "(missing)",
                    }
                )
                logger.warning(
                    "integrity breach: task %s — in task.json but "
                    "missing from SQLite, json status=%s",
                    task_id,
                    json_status,
                )
                if on_breach is not None:
                    on_breach(task_id, json_status, "(missing)")

        return breaches

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
