"""Tests for the SQLite migration engine (acp.evidence.migrations).

Covers:
  - get_store_version on fresh and migrated DBs
  - run_migrations: fresh, idempotent, partial, rollback-on-failure
  - DurableEventStore / DurableTaskStore init sets store version
  - Schema additions (signature_algorithm, orphan_reason)
  - Old single-PK schema migration to composite PK
  - Data preservation across migrations
  - needs_rebuild for valid and corrupted DBs
  - Migration progress logging
  - Migration list structure (list[list[str]])
  - Multi-store coexistence (events + tasks in same DB file)
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from acp.events import GENESIS_HASH
from acp.evidence.durable_store import DurableEventStore
from acp.evidence.durable_task_store import DurableTaskStore
from acp.evidence.migrations import (
    EVENT_STORE_MIGRATIONS,
    TASK_STORE_MIGRATIONS,
    get_store_version,
    needs_rebuild,
    run_migrations,
)
from acp.models import Event, EventType


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open an autocommit-mode SQLite connection (as the migration engine expects)."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    return conn


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


# --------------------------------------------------------------------------- #
# Low-level migration engine tests
# --------------------------------------------------------------------------- #


def test_get_store_version_new_db(tmp_path: Path):
    conn = _connect(tmp_path / "test.db")
    assert get_store_version(conn, "test") == 0
    conn.close()


def test_run_migrations_fresh_db(tmp_path: Path):
    conn = _connect(tmp_path / "test.db")
    migrations = [
        ["CREATE TABLE foo (id INTEGER PRIMARY KEY)"],
        ["ALTER TABLE foo ADD COLUMN bar TEXT"],
    ]
    new_version = run_migrations(conn, migrations, store_name="test")
    assert new_version == len(migrations)
    assert get_store_version(conn, "test") == len(migrations)
    conn.close()


def test_run_migrations_idempotent(tmp_path: Path):
    conn = _connect(tmp_path / "test.db")
    migrations = [
        ["CREATE TABLE foo (id INTEGER PRIMARY KEY)"],
        ["ALTER TABLE foo ADD COLUMN bar TEXT"],
    ]
    run_migrations(conn, migrations, store_name="test")
    # Running again should be a no-op and return the current version.
    new_version = run_migrations(conn, migrations, store_name="test")
    assert new_version == len(migrations)
    assert get_store_version(conn, "test") == len(migrations)
    conn.close()


def test_run_migrations_partial(tmp_path: Path):
    conn = _connect(tmp_path / "test.db")
    migrations = [
        ["CREATE TABLE foo (id INTEGER PRIMARY KEY)"],
        ["ALTER TABLE foo ADD COLUMN bar TEXT"],
        ["ALTER TABLE foo ADD COLUMN baz TEXT"],
    ]
    # Apply only migration 0 (version 0 → 1).
    run_migrations(conn, migrations[:1], store_name="test")
    assert get_store_version(conn, "test") == 1

    # Run full migrations — only migrations 1 and 2 should run.
    new_version = run_migrations(conn, migrations, store_name="test")
    assert new_version == 3
    # Verify the columns added by migrations 1 and 2 exist.
    cols = [c[1] for c in conn.execute("PRAGMA table_info(foo)").fetchall()]
    assert "bar" in cols
    assert "baz" in cols
    conn.close()


def test_run_migrations_rollback_on_failure(tmp_path: Path):
    conn = _connect(tmp_path / "test.db")
    migrations = [
        ["CREATE TABLE foo (id INTEGER PRIMARY KEY)"],
        ["ALTER TABLE foo ADD COLUMN bar TEXT", "SELECT no_such_column FROM foo"],
    ]
    # Apply migration 0 successfully.
    run_migrations(conn, migrations[:1], store_name="test")
    assert get_store_version(conn, "test") == 1

    # Migration 1 should fail on the bad SQL and roll back.
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(conn, migrations, store_name="test")

    # Store version should remain at 1 (rollback).
    assert get_store_version(conn, "test") == 1
    # The 'bar' column should NOT exist because the transaction was rolled back.
    cols = [c[1] for c in conn.execute("PRAGMA table_info(foo)").fetchall()]
    assert "bar" not in cols
    conn.close()


# --------------------------------------------------------------------------- #
# Integration: DurableEventStore / DurableTaskStore init
# --------------------------------------------------------------------------- #


def test_event_store_init_sets_version(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    conn = sqlite3.connect(str(tmp_path / "events.db"))
    assert get_store_version(conn, "event_store") == len(EVENT_STORE_MIGRATIONS)
    conn.close()
    db.close()


def test_task_store_init_sets_version(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    conn = sqlite3.connect(str(tmp_path / "tasks.db"))
    assert get_store_version(conn, "task_store") == len(TASK_STORE_MIGRATIONS)
    conn.close()
    db.close()


def test_event_store_migration_adds_signature_algorithm(tmp_path: Path):
    db = DurableEventStore(tmp_path / "events.db")
    db.init()
    conn = sqlite3.connect(str(tmp_path / "events.db"))
    cols = [c[1] for c in conn.execute("PRAGMA table_info(events)").fetchall()]
    assert "signature_algorithm" in cols
    conn.close()
    db.close()


def test_task_store_migration_adds_orphan_reason(tmp_path: Path):
    db = DurableTaskStore(tmp_path / "tasks.db")
    db.init()
    conn = sqlite3.connect(str(tmp_path / "tasks.db"))
    cols = [c[1] for c in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    assert "orphan_reason" in cols
    conn.close()
    db.close()


def test_old_schema_migration(tmp_path: Path):
    db_path = tmp_path / "events.db"
    # Create a DB with the old single-PK schema (event_id TEXT PRIMARY KEY).
    conn = _connect(db_path)
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, task_id TEXT, type TEXT, "
        "timestamp TEXT, payload TEXT, prev_hash TEXT, hash TEXT, signature TEXT)"
    )
    conn.close()

    # Now call DurableEventStore.init() — it should detect the old PK and migrate.
    db = DurableEventStore(db_path)
    db.init()

    verify_conn = sqlite3.connect(str(db_path))
    # Store version should be updated to the latest migration count.
    assert get_store_version(verify_conn, "event_store") == len(EVENT_STORE_MIGRATIONS)
    # The PK should now be composite (task_id, event_id).
    pk_cols = [c[1] for c in verify_conn.execute("PRAGMA table_info(events)").fetchall() if c[5]]
    assert pk_cols == ["task_id", "event_id"]
    verify_conn.close()
    db.close()


def test_migration_preserves_data(tmp_path: Path):
    db_path = tmp_path / "events.db"
    # Create a DB at version 1 (only migration 0 applied) with the composite PK.
    conn = _connect(db_path)
    run_migrations(conn, EVENT_STORE_MIGRATIONS[:1], store_name="event_store")
    assert get_store_version(conn, "event_store") == 1

    # Insert an event at v1 schema (no signature_algorithm column yet).
    conn.execute(
        "INSERT INTO events (task_id, event_id, type, timestamp, payload, "
        "prev_hash, hash, signature) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task_001",
            "evt_000001",
            "task.created",
            "2026-06-24T10:00:00Z",
            "{}",
            GENESIS_HASH,
            "h1",
            "",
        ),
    )
    conn.close()

    # Now run the full migrations — migration 1 adds signature_algorithm.
    conn = _connect(db_path)
    run_migrations(conn, EVENT_STORE_MIGRATIONS, store_name="event_store")
    assert get_store_version(conn, "event_store") == len(EVENT_STORE_MIGRATIONS)

    # The event should still be present.
    row = conn.execute(
        "SELECT task_id, event_id FROM events WHERE task_id = ?", ("task_001",)
    ).fetchone()
    assert row is not None
    assert row[0] == "task_001"
    assert row[1] == "evt_000001"
    conn.close()


# --------------------------------------------------------------------------- #
# needs_rebuild
# --------------------------------------------------------------------------- #


def test_needs_rebuild_valid_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = _connect(db_path)
    conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    assert needs_rebuild(conn) is False
    conn.close()


def test_needs_rebuild_corrupted_db(tmp_path: Path):
    db_path = tmp_path / "corrupt.db"
    # Write garbage that is not a valid SQLite database.
    db_path.write_bytes(b"NOT A SQLITE DATABASE" * 100)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    assert needs_rebuild(conn) is True
    conn.close()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def test_migration_logs_progress(tmp_path: Path, caplog):
    conn = _connect(tmp_path / "test.db")
    migrations = [
        ["CREATE TABLE foo (id INTEGER PRIMARY KEY)"],
        ["ALTER TABLE foo ADD COLUMN bar TEXT"],
    ]
    with caplog.at_level(logging.INFO, logger="acp.evidence.migrations"):
        run_migrations(conn, migrations, store_name="test_store")
    conn.close()

    # Verify the "migrating schema" info message was logged.
    messages = [r.message for r in caplog.records]
    assert any("migrating schema" in m and "test_store" in m for m in messages)
    # Verify per-migration progress messages were logged.
    assert any("migration 0 → 1 applied" in m for m in messages)
    assert any("migration 1 → 2 applied" in m for m in messages)


# --------------------------------------------------------------------------- #
# Migration list structure
# --------------------------------------------------------------------------- #


def test_event_store_migrations_list_immutable():
    assert isinstance(EVENT_STORE_MIGRATIONS, list)
    for migration in EVENT_STORE_MIGRATIONS:
        assert isinstance(migration, list)
        for sql in migration:
            assert isinstance(sql, str)
            assert len(sql) > 0


def test_task_store_migrations_list_immutable():
    assert isinstance(TASK_STORE_MIGRATIONS, list)
    for migration in TASK_STORE_MIGRATIONS:
        assert isinstance(migration, list)
        for sql in migration:
            assert isinstance(sql, str)
            assert len(sql) > 0


# --------------------------------------------------------------------------- #
# Multi-store coexistence (events + tasks in same DB file)
# --------------------------------------------------------------------------- #


def test_multi_store_coexistence(tmp_path: Path):
    """Both DurableEventStore and DurableTaskStore can share one DB file.

    This is the critical v0.7.2 fix: using PRAGMA user_version (a single
    integer per database) caused the second store's migrations to be
    skipped because the first store already set user_version. The
    schema_versions table tracks per-store versions independently.
    """
    db_path = tmp_path / "shared.db"

    # Initialize task store first (as the workflow does).
    task_store = DurableTaskStore(db_path)
    task_store.init()

    # Initialize event store on the SAME database file.
    event_store = DurableEventStore(db_path)
    event_store.init()

    # Both tables must exist.
    conn = sqlite3.connect(str(db_path))
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    assert "tasks" in tables
    assert "events" in tables
    assert "schema_versions" in tables

    # Both stores should have their own version tracked independently.
    assert get_store_version(conn, "task_store") == len(TASK_STORE_MIGRATIONS)
    assert get_store_version(conn, "event_store") == len(EVENT_STORE_MIGRATIONS)
    conn.close()

    task_store.close()
    event_store.close()
