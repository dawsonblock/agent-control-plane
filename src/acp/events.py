"""Event log — the source of truth.

Every meaningful action in a run appends one event to
``data/runs/<task_id>/events.jsonl``. The report is the human-readable
projection of this log; Graphiti memory is derived from it. If it's not
in this file, it didn't happen.

Events form a hash chain (v0.5.5): each event's ``hash`` is sha256 of
``prev_hash + event_id + task_id + type + timestamp + payload``. The first
event's ``prev_hash`` is the literal string ``"GENESIS"``. This makes the
log tamper-evident — any removal, reordering, or modification breaks the
chain and is detectable by :func:`verify_event_chain`.

Each event write is fsync'd (v0.5.6) so a crash mid-run cannot produce a
partial last line. The file is opened in append+binary mode, the line is
written, the buffer is flushed, and ``os.fsync`` is called on the file
descriptor before the write returns. This makes the log crash-safe: every
event that returned from :meth:`EventWriter.write` is on disk.

Optional Ed25519 signing (v0.5.6): if a signing key is provided to
:meth:`EventWriter.set_signing_key`, each event's ``signature`` field is
an Ed25519 signature over the event's hash. This proves *authenticity*
(who wrote the log), not just *integrity* (the log hasn't been modified).
Verification: :func:`verify_event_signatures`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC
from pathlib import Path
from typing import Any

from acp.models import Event, EventType, next_event_id

logger = logging.getLogger(__name__)

GENESIS_HASH = "GENESIS"


def _compute_event_hash(
    *,
    prev_hash: str,
    event_id: str,
    task_id: str,
    event_type: str,
    timestamp: str,
    payload: dict[str, Any],
) -> str:
    """sha256 of the canonical-JSON encoding of the event's chain fields."""
    content = json.dumps(
        {
            "prev_hash": prev_hash,
            "event_id": event_id,
            "task_id": task_id,
            "type": event_type,
            "timestamp": timestamp,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode()).hexdigest()


def verify_event_chain(events: list[Event]) -> bool:
    """Verify the hash chain of an event list.

    Returns ``True`` iff every event's ``prev_hash`` matches the preceding
    event's ``hash`` and every event's ``hash`` matches the recomputed
    value. The first event must have ``prev_hash == "GENESIS"``.

    An empty event list returns ``False`` — a run with zero events has no
    evidence trail to verify and is likely a deleted or tampered log.
    """
    if not events:
        return False
    prev = GENESIS_HASH
    for evt in events:
        if evt.prev_hash != prev:
            return False
        expected = _compute_event_hash(
            prev_hash=evt.prev_hash,
            event_id=evt.event_id,
            task_id=evt.task_id,
            event_type=evt.type.value,
            timestamp=evt.timestamp,
            payload=evt.payload,
        )
        if evt.hash != expected:
            return False
        prev = evt.hash
    return True


def verify_event_signatures(events: list[Event], public_key: bytes) -> bool:
    """Verify Ed25519 signatures on all events.

    Returns ``True`` iff every event has a ``signature`` field that is a
    valid Ed25519 signature over the event's ``hash``, verified against
    the provided ``public_key``. Events without signatures fail
    verification. Requires the ``cryptography`` package.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise ImportError(
            "Ed25519 signature verification requires the 'cryptography' package. "
            "Install it with: uv sync --extra crypto"
        )
    pk = Ed25519PublicKey.from_public_bytes(public_key)
    if not events:
        return False
    for evt in events:
        if not evt.signature:
            return False
        try:
            sig_bytes = bytes.fromhex(evt.signature)
            pk.verify(sig_bytes, evt.hash.encode())
        except Exception:  # noqa: BLE001
            return False
    return True


class EventWriter:
    """Append-only, fsync'd, hash-chained writer for a task's event log.

    One writer per task. Thread-unsafe by design: a task runs linearly (M1)
    or under a single graph invocation (M3).

    Each write is fsync'd to disk before returning, so a crash cannot
    produce a partial last line. An optional Ed25519 signing key can be
    set via :meth:`set_signing_key` to add authenticity proofs.
    """

    def __init__(self, task_id: str, run_dir: Path) -> None:
        self.task_id = task_id
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "events.jsonl"
        self._count = 0
        self._prev_hash = GENESIS_HASH
        self._signing_key: Any = None  # Ed25519PrivateKey or None
        if self.path.exists():
            # Resume counting + hash chain after a restart / repair attempt.
            for line in self.path.open():
                if not line.strip():
                    continue
                self._count += 1
                try:
                    evt = Event.model_validate_json(line)
                    self._prev_hash = evt.hash or GENESIS_HASH
                except Exception as exc:  # noqa: BLE001
                    logger.warning("malformed event line during hash recompute: %s", exc)

    def set_signing_key(self, private_key: bytes) -> None:
        """Set an Ed25519 private key for signing events.

        After this is called, every event written will include a
        ``signature`` field (Ed25519 signature over the event's hash).
        Requires the ``cryptography`` package. The key must be exactly 32
        bytes (Ed25519 raw private key format).
        """
        if len(private_key) != 32:
            raise ValueError(
                f"Ed25519 private key must be exactly 32 bytes, got {len(private_key)}"
            )
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            raise ImportError(
                "Ed25519 signing requires the 'cryptography' package. "
                "Install it with: uv sync --extra crypto"
            )
        self._signing_key = Ed25519PrivateKey.from_private_bytes(private_key)

    def relocate(self, task_id: str, run_dir: Path) -> None:
        """Repoint this writer at a task's real run dir.

        Used by the LangGraph entry node: the writer is constructed before
        the task id is known (the id is minted inside ``create_task``), so
        ``create_task`` calls ``relocate`` once it has the real id. Any
        events written before relocation are re-pointed in memory; in
        practice nothing is written before ``create_task`` runs.
        """
        self.task_id = task_id
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "events.jsonl"
        self._count = 0
        self._prev_hash = GENESIS_HASH
        if self.path.exists():
            for line in self.path.open():
                if not line.strip():
                    continue
                self._count += 1
                try:
                    evt = Event.model_validate_json(line)
                    self._prev_hash = evt.hash or GENESIS_HASH
                except Exception as exc:  # noqa: BLE001
                    logger.warning("skipping malformed event line during hash recompute: %s", exc)

    def write(self, type: EventType, payload: dict[str, Any] | None = None) -> Event:
        """Append one event, fsync it to disk, and return the Event object.

        The write is crash-safe: the file is flushed and fsync'd before
        this method returns. If a signing key is set, the event includes
        an Ed25519 signature over its hash.
        """
        event = self.build_event(type, payload)
        self.append_event(event)
        return event

    def build_event(self, type: EventType, payload: dict[str, Any] | None = None) -> Event:
        """Build an Event object with the correct hash chain fields, WITHOUT writing to disk.

        v0.7.4: Used by the lifecycle transaction in durable_mode="required"
        to write to SQLite first, then append to JSONL. This separates
        event construction from event persistence, enabling crash-safe
        ordering: SQLite commit → JSONL append (instead of JSONL append →
        SQLite commit → JSONL truncate on failure).

        The caller is responsible for calling :meth:`append_event` to
        actually write the event to disk.
        """
        event_id = next_event_id(self._count)
        timestamp = _utcnow_iso()
        payload = payload or {}
        hash_value = _compute_event_hash(
            prev_hash=self._prev_hash,
            event_id=event_id,
            task_id=self.task_id,
            event_type=type.value,
            timestamp=timestamp,
            payload=payload,
        )
        signature = ""
        if self._signing_key is not None:
            signature = self._signing_key.sign(hash_value.encode()).hex()
        return Event(
            event_id=event_id,
            task_id=self.task_id,
            type=type,
            timestamp=timestamp,
            payload=payload,
            prev_hash=self._prev_hash,
            hash=hash_value,
            signature=signature,
        )

    def append_event(self, event: Event) -> None:
        """Write a pre-built Event to disk and update internal state.

        v0.7.4: Counterpart to :meth:`build_event`. Writes the event to
        events.jsonl with fsync, and updates the count and prev_hash.
        This is the persistence step that was previously inlined in
        :meth:`write`.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # Crash-safe append: open in binary append mode, write, flush, fsync.
        with self.path.open("ab") as f:
            f.write((event.model_dump_json() + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        self._count += 1
        self._prev_hash = event.hash

    def read_all(self) -> list[Event]:
        """Read every event in log order. Used by the report writer.

        Malformed lines are skipped — a corrupt line in the middle of the log
        should not prevent reading the valid events before and after it. The
        hash chain may be broken at that point, but ``verify_event_chain``
        will catch that during verification.
        """
        if not self.path.exists():
            return []
        events: list[Event] = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                events.append(Event.model_validate_json(line))
            except Exception as exc:  # noqa: BLE001
                logger.warning("skipping malformed event line in read_all: %s", exc)
                continue
        return events

    @property
    def count(self) -> int:
        return self._count

    @property
    def last_hash(self) -> str:
        """The hash of the most recently written event (or GENESIS)."""
        return self._prev_hash

    def checkpoint(self) -> tuple[int, int, str]:
        """Capture the writer's current state for potential rollback.

        Returns ``(file_size, count, prev_hash)``. Pass this to
        :meth:`rollback` to undo events written after the checkpoint.

        Used by lifecycle commands (approve/reject) to implement atomicity:
        if a durable store write fails after the lifecycle event has been
        appended to the JSONL log, the caller can rollback to remove the
        event and restore the log to its pre-lifecycle state.
        """
        file_size = self.path.stat().st_size if self.path.exists() else 0
        return (file_size, self._count, self._prev_hash)

    def rollback(self, checkpoint: tuple[int, int, str]) -> None:
        """Undo events written after the given checkpoint.

        Truncates the JSONL log to the checkpoint's file size and restores
        the internal count + prev_hash. This is the only situation where
        the append-only log is truncated: a failed transactional write that
        must not leave a partial event behind.

        After rollback, the log is in the same state it was before the
        events that were rolled back — the hash chain is intact because
        ``prev_hash`` is restored to the checkpoint's value.
        """
        file_size, count, prev_hash = checkpoint
        # Truncate the file to the checkpoint size.
        with self.path.open("ab") as f:
            f.truncate(file_size)
            f.flush()
            os.fsync(f.fileno())
        self._count = count
        self._prev_hash = prev_hash


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with a trailing Z."""
    from datetime import datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
