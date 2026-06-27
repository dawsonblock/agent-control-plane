"""Lightweight SQLite migration engine — forward-rolling schema updates.

Uses a ``schema_versions`` table to track per-store schema state without
needing a heavy ORM like SQLAlchemy/Alembic. The JSONL event log remains
the canonical source of truth; the SQLite store is a derived index.

Migration principles:
  1. Migrations are **forward-only** — no down-migrations. The JSONL log
     is the source of truth, so a full rebuild is always possible.
  2. Each migration runs in a ``BEGIN EXCLUSIVE`` transaction so concurrent
     processes can't race on schema updates.
  3. The migration list is **immutable** — once a migration is published,
     it never changes. New schema changes append to the list.
  4. ``rebuild_from_jsonl`` is a fallback for catastrophic corruption only,
     not the default schema-update strategy.

v0.7.2 note: We use a ``schema_versions`` table instead of ``PRAGMA
user_version`` because multiple stores (events + tasks) share the same
SQLite database file. ``PRAGMA user_version`` is a single integer per
database, so it can't track per-store versions independently.

Usage::

    from acp.evidence.migrations import run_migrations

    conn = sqlite3.connect(db_path, isolation_level=None)
    run_migrations(conn, EVENT_STORE_MIGRATIONS, store_name="event_store")
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _ensure_schema_versions_table(conn: sqlite3.Connection) -> None:
    """Create the schema_versions table if it doesn't exist.

    This table tracks the migration version per store name:

        CREATE TABLE schema_versions (
            store_name  TEXT PRIMARY KEY,
            version     INTEGER NOT NULL DEFAULT 0
        )
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            store_name  TEXT PRIMARY KEY,
            version     INTEGER NOT NULL DEFAULT 0
        )
    """)


# --------------------------------------------------------------------------- #
# Event store migrations
# --------------------------------------------------------------------------- #
# Each migration is a single SQL string (or list of SQL strings) executed
# within one transaction. The index in the list is the migration version
# (0-based: MIGRATIONS[0] brings user_version from 0 → 1).

EVENT_STORE_MIGRATIONS: list[list[str]] = [
    # Migration 0 → 1: Initial schema (composite primary key).
    # This is the v0.5.11+ schema. If the database already has the events
    # table (from the old init path), this migration is a no-op because
    # of CREATE TABLE IF NOT EXISTS.
    [
        """
        CREATE TABLE IF NOT EXISTS events (
            task_id    TEXT NOT NULL,
            event_id   TEXT NOT NULL,
            type       TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            payload    TEXT NOT NULL,
            prev_hash  TEXT NOT NULL,
            hash       TEXT NOT NULL,
            signature  TEXT DEFAULT '',
            PRIMARY KEY (task_id, event_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
        "CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp)",
    ],
    # Migration 1 → 2: Add signature_algorithm column for future
    # multi-algorithm support (Ed25519 now, ML-DSA later). Defaults to
    # 'ed25519' for existing rows (backward compatible).
    [
        "ALTER TABLE events ADD COLUMN signature_algorithm TEXT DEFAULT 'ed25519'",
    ],
]

# --------------------------------------------------------------------------- #
# Task store migrations
# --------------------------------------------------------------------------- #

TASK_STORE_MIGRATIONS: list[list[str]] = [
    # Migration 0 → 1: Initial schema.
    [
        """
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
        """,
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_repo ON tasks(repo_name)",
    ],
    # Migration 1 → 2: Add orphan_reason column for tracking why a task
    # was marked as orphaned (server restart, crash, manual intervention).
    [
        "ALTER TABLE tasks ADD COLUMN orphan_reason TEXT DEFAULT ''",
    ],
    # Migration 2 → 3: v0.9.0 (Step 7) — Mission context columns so the
    # durable Task carries the overarching mission goal/description. This
    # makes Graphiti memory extraction mission-aware on the
    # ``task_store_primary="sqlite"`` path (the json path gets them via
    # model_dump). All default to empty/0 so existing rows are back-compat.
    [
        "ALTER TABLE tasks ADD COLUMN mission_id TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN mission_goal TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN mission_description TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN mission_step_index INTEGER DEFAULT 0",
    ],
]


# --------------------------------------------------------------------------- #
# Migration engine
# --------------------------------------------------------------------------- #


def get_store_version(conn: sqlite3.Connection, store_name: str) -> int:
    """Read the current schema version for a store from the schema_versions table.

    Args:
        conn: A SQLite connection.
        store_name: The store name (e.g., "event_store", "task_store").

    Returns:
        The version integer (0 if the store has no recorded version).
    """
    _ensure_schema_versions_table(conn)
    row = conn.execute(
        "SELECT version FROM schema_versions WHERE store_name = ?", (store_name,)
    ).fetchone()
    return int(row[0]) if row else 0


def run_migrations(
    conn: sqlite3.Connection,
    migrations: list[list[str]],
    *,
    store_name: str = "unknown",
) -> int:
    """Run pending migrations, updating the per-store schema version.

    Args:
        conn: A SQLite connection in autocommit mode
            (``isolation_level=None``).
        migrations: The immutable list of migration SQL groups.
        store_name: Unique name for this store (e.g., "event_store").
            Used as the key in the ``schema_versions`` table.

    Returns:
        The new schema version after migrations.

    If the store is at version ``V`` and there are ``N`` migrations,
    runs migrations ``V`` through ``N-1`` (0-indexed), each in its own
    ``BEGIN EXCLUSIVE`` transaction. After each migration, updates the
    ``schema_versions`` table to ``V+1``.

    If a migration fails, the transaction is rolled back and the error
    is re-raised. The database remains at the last successfully applied
    version.
    """
    _ensure_schema_versions_table(conn)
    current = get_store_version(conn, store_name)
    target = len(migrations)

    if current >= target:
        logger.debug(
            "%s: schema up to date (version %d, target %d)",
            store_name,
            current,
            target,
        )
        return current

    logger.info(
        "%s: migrating schema from version %d to %d (%d migration(s))",
        store_name,
        current,
        target,
        target - current,
    )

    for i in range(current, target):
        migration_sqls = migrations[i]
        try:
            conn.execute("BEGIN EXCLUSIVE")
            for sql in migration_sqls:
                conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_versions (store_name, version) VALUES (?, ?) "
                "ON CONFLICT(store_name) DO UPDATE SET version = ?",
                (store_name, i + 1, i + 1),
            )
            conn.execute("COMMIT")
            logger.info("%s: migration %d → %d applied", store_name, i, i + 1)
        except Exception as exc:
            conn.execute("ROLLBACK")
            logger.error(
                "%s: migration %d → %d failed: %s — database remains at version %d",
                store_name,
                i,
                i + 1,
                exc,
                i,
            )
            raise

    return target


def needs_rebuild(conn: sqlite3.Connection) -> bool:
    """Check if the database is catastrophically corrupted and needs rebuild.

    Returns True if the database file is not a valid SQLite database
    (e.g., truncated, overwritten with non-SQLite content). This is the
    only condition under which ``rebuild_from_jsonl`` should be invoked
    as a schema-update strategy — normal migrations handle schema changes.
    """
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return False
    except sqlite3.DatabaseError:
        return True
