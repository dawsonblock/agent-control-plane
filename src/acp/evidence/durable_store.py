"""SQLite durable event store — transactional, queryable event persistence.

The JSONL event log is the primary store (simple, append-only, human-readable).
This module provides an optional SQLite-backed store for operators who need:

  * **Transactional durability**: SQLite's WAL mode + fsync gives stronger
    crash guarantees than a single JSONL append, especially under concurrent
    access.
  * **Queryability**: query events by type, task, time range, or payload
    content without scanning a file.
  * **Cross-task indexing**: a single database can hold events from many
    tasks, with indexes on task_id, type, and timestamp.

The SQLite store is **additive**, not a replacement: when enabled, events
are written to both the JSONL file (the canonical source) and the SQLite
database (the queryable index). The JSONL file remains the source of truth;
the SQLite store is a derived index that can be rebuilt from it.

Usage::

    store = DurableEventStore(db_path)
    store.init()
    for event in events:
        store.append(event)
    # Query
    failures = store.query(task_id="task_20260624_0001", type="task.failed")

Schema:

    CREATE TABLE events (
        task_id    TEXT NOT NULL,
        event_id   TEXT NOT NULL,
        type       TEXT NOT NULL,
        timestamp  TEXT NOT NULL,
        payload    TEXT NOT NULL,  -- JSON
        prev_hash  TEXT NOT NULL,
        hash       TEXT NOT NULL,
        signature  TEXT DEFAULT '',
        PRIMARY KEY (task_id, event_id)
    );
    CREATE INDEX idx_events_task ON events(task_id);
    CREATE INDEX idx_events_type ON events(type);
    CREATE INDEX idx_events_time ON events(timestamp);

The composite primary key (task_id, event_id) is critical: event IDs are
per-task (evt_000001, evt_000002, ...), so a single-task primary key would
collide across tasks. The composite key ensures every (task_id, event_id)
pair is unique, allowing a single SQLite database to hold events from many
tasks without collision.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from acp.models import Event, EventType


class DurableEventStore:
    """SQLite-backed event store — transactional, queryable, crash-safe.

    Uses WAL mode for concurrent read access and fsync-on-commit for
    durability. The store is additive to the JSONL log (which remains
    the canonical source).
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """Initialize the database schema. Idempotent.

        Uses the forward-rolling migration engine (``acp.evidence.migrations``)
        to apply schema updates via ``PRAGMA user_version``. This avoids the
        O(N) drop-and-rebuild-from-JSONL strategy as the database grows.

        Handles migration from the old single-column primary key schema
        (event_id TEXT PRIMARY KEY) to the composite key schema
        (PRIMARY KEY (task_id, event_id)). If the old schema is detected
        (user_version == 0 but the events table exists with the old PK),
        the table is dropped once — the JSONL log is the canonical source,
        so the SQLite store can be safely rebuilt. After that, normal
        migrations proceed.
        """
        from acp.evidence.migrations import EVENT_STORE_MIGRATIONS, run_migrations

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,  # autocommit mode; we manage txns explicitly
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        # Check for old schema (event_id as sole PRIMARY KEY) and migrate.
        # This handles databases created before the migration engine existed
        # (user_version == 0 but table already present with old PK layout).
        cols = self._conn.execute("PRAGMA table_info(events)").fetchall()
        if cols:
            pk_cols = [c[1] for c in cols if c[5]]  # c[5] is pk flag
            if pk_cols == ["event_id"]:
                # Old schema — drop and recreate with composite key.
                # The JSONL log is canonical; SQLite is a derived index.
                self._conn.execute("DROP TABLE IF EXISTS events")

        # Run forward-rolling migrations via PRAGMA user_version.
        run_migrations(self._conn, EVENT_STORE_MIGRATIONS, store_name="event_store")

    def append(self, event: Event) -> None:
        """Insert one event. Raises if (task_id, event_id) already exists."""
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        self._conn.execute(
            "INSERT INTO events (task_id, event_id, type, timestamp, payload, prev_hash, hash, signature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.task_id,
                event.event_id,
                event.type.value,
                event.timestamp,
                json.dumps(event.payload, sort_keys=True),
                event.prev_hash,
                event.hash,
                event.signature,
            ),
        )

    def append_batch(self, events: list[Event]) -> None:
        """Insert multiple events in a single transaction."""
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        self._conn.execute("BEGIN")
        try:
            for event in events:
                self._conn.execute(
                    "INSERT INTO events (task_id, event_id, type, timestamp, payload, prev_hash, hash, signature) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.task_id,
                        event.event_id,
                        event.type.value,
                        event.timestamp,
                        json.dumps(event.payload, sort_keys=True),
                        event.prev_hash,
                        event.hash,
                        event.signature,
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def begin_transaction(self) -> None:
        """Begin an explicit transaction. Use with :meth:`append` + :meth:`commit`
        or :meth:`rollback_transaction` for multi-event atomic writes.

        The connection is in autocommit mode (``isolation_level=None``), so
        ``BEGIN`` must be issued explicitly. All subsequent ``append`` calls
        are part of the transaction until :meth:`commit` or
        :meth:`rollback_transaction` is called.
        """
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        """Commit the current explicit transaction."""
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        self._conn.execute("COMMIT")

    def rollback_transaction(self) -> None:
        """Roll back the current explicit transaction."""
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        self._conn.execute("ROLLBACK")

    def query(
        self,
        *,
        task_id: str | None = None,
        type: str | EventType | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        """Query events by task, type, and/or time range.

        Returns events in log order (by event_id). ``since`` and ``until``
        are ISO-8601 timestamps (inclusive).
        """
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if type is not None:
            type_str = type.value if isinstance(type, EventType) else type
            clauses.append("type = ?")
            params.append(type_str)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT task_id, event_id, type, timestamp, payload, prev_hash, hash, signature "
            f"FROM events{where} ORDER BY task_id ASC, event_id ASC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def count(self, *, task_id: str | None = None) -> int:
        """Count events, optionally filtered by task_id."""
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        if task_id is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return row[0] if row else 0

    def rebuild_from_jsonl(self, jsonl_path: Path | str) -> int:
        """Rebuild the SQLite store from a JSONL event log.

        Drops all existing events and re-imports from the JSONL file.
        Returns the number of events imported. Useful for migrating from
        JSONL-only to SQLite-backed operation.
        """
        if self._conn is None:
            raise RuntimeError("DurableEventStore not initialized — call .init() first")
        self._conn.execute("DELETE FROM events")
        jsonl_path = Path(jsonl_path)
        if not jsonl_path.is_file():
            return 0
        events = [
            Event.model_validate_json(line)
            for line in jsonl_path.read_text().splitlines()
            if line.strip()
        ]
        self.append_batch(events)
        return len(events)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> DurableEventStore:
        self.init()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _row_to_event(row: tuple) -> Event:
    """Convert a SQLite row to an Event model.

    Column order matches the SELECT statements: task_id, event_id, type,
    timestamp, payload, prev_hash, hash, signature.
    """
    return Event(
        task_id=row[0],
        event_id=row[1],
        type=EventType(row[2]),
        timestamp=row[3],
        payload=json.loads(row[4]),
        prev_hash=row[5],
        hash=row[6],
        signature=row[7] or "",
    )
