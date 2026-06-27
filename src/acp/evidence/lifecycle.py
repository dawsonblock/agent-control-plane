"""Lifecycle event service — shared by CLI and API.

This module provides the transactional lifecycle event writer used by both
``acp approve`` / ``acp reject`` (CLI) and ``POST /tasks/{id}/approve`` /
``POST /tasks/{id}/reject`` (API).  Extracting it ensures the API does not
bypass the cryptographic bindings, SQLite dual-writes, manifest re-renders,
and rollback safety that the CLI enforces.

Two public functions:

* ``record_lifecycle_event`` — append a post-run lifecycle event
  (human.approved / human.rejected) with full transactional integrity.

* ``rerender_vault_note_from_state`` — re-render a vault note from scratch
  as a pure projection of the current event log, report, and task state.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from acp.events import EventWriter
from acp.models import EventType
from acp.store import TaskStore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Vault note re-rendering
# --------------------------------------------------------------------------- #


def rerender_vault_note_from_state(
    *,
    note_path: Path,
    run_dir: Path,
    task: Any,
    store: TaskStore,
    vault_root: Path,
    on_warning: Any = None,
) -> None:
    """Re-render a vault note from scratch as a pure projection of state.

    Reads the current event log, report, and task state, then rebuilds the
    vault note entirely.  The note's frontmatter (approved, memory_status,
    audit_trail) is derived from the event log, not modified in-place.

    Best-effort: if re-rendering fails, the vault note keeps its current
    content.  The event log is authoritative; the note is a human convenience.

    ``on_warning`` is an optional callback invoked with a message string if
    re-rendering fails.  The CLI passes a Rich console printer; the API
    can pass a logger or ignore it.
    """
    try:
        from acp.gitops.diff import DiffCapture
        from acp.models import Recommendation, ReviewResult, RiskLevel
        from acp.vault.obsidian_writer import rerender_vault_note

        # Read the current report.
        report_path = run_dir / "artifacts" / "final_report.md"
        report_body = report_path.read_text() if report_path.is_file() else ""

        # Read the current event log.
        events_writer = EventWriter(task.task_id, run_dir)
        events = events_writer.read_all()

        # Read the review result if it exists.
        review_path = run_dir / "artifacts" / "review.json"
        review = ReviewResult(risk=RiskLevel.LOW, recommendation=Recommendation.MERGE)
        if review_path.is_file():
            try:
                review = ReviewResult.model_validate_json(review_path.read_text())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to parse review.json for vault note re-render: %s — "
                    "using default review (low risk, merge)",
                    exc,
                )

        # Build a minimal diff capture for frontmatter stats.
        diff = DiffCapture(
            patch="",
            stat="",
            changed_files=[],
            insertions=0,
            deletions=0,
        )
        diff_stat_path = run_dir / "artifacts" / "diff_stat.txt"
        if diff_stat_path.is_file():
            from acp.gitops.diff import _parse_stat

            try:
                changed, ins, dels = _parse_stat(diff_stat_path.read_text())
                diff = DiffCapture(
                    patch="",
                    stat=diff_stat_path.read_text(),
                    changed_files=changed,
                    insertions=ins,
                    deletions=dels,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to parse diff stat for vault note re-render: %s — using empty diff",
                    exc,
                )

        rerender_vault_note(
            note_path=note_path,
            report_body=report_body,
            task=task,
            review=review,
            diff=diff,
            events=events,
            vault_root=vault_root,
        )
    except Exception as exc:  # noqa: BLE001
        if on_warning is not None:
            on_warning(
                f"vault note re-render failed: {exc}\n"
                "    event log is authoritative; note may be stale"
            )


# --------------------------------------------------------------------------- #
# Lifecycle event recording (transactional)
# --------------------------------------------------------------------------- #


def record_lifecycle_event(
    *,
    task_id: str,
    run_dir: Path,
    event_type: EventType,
    payload: dict[str, Any],
) -> str | None:
    """Append a post-run lifecycle event with full transactional integrity.

    All evidence mutations — events.jsonl, final_report.md,
    lifecycle_manifest.json, and the SQLite durable store — are staged
    and committed as one transaction.  If ANY step fails (especially a
    required-mode durable store write), ALL evidence files are restored
    to their pre-lifecycle state.  No partial state survives.

    Returns a warning string if the durable store write failed in
    best_effort mode, or ``None`` if everything succeeded.  In required
    mode, any failure raises ``ACPError`` after rolling back all evidence.
    """
    from acp.errors import ACPError, EvidenceConfigError
    from acp.evidence.manifest import (
        compute_report_hash,
        read_evidence_config,
        write_lifecycle_manifest,
    )

    events = EventWriter(task_id, run_dir)
    ev_cfg = read_evidence_config(run_dir)

    # Determine whether the run was signed.  The evidence_config sidecar is the
    # primary signal (written at finalize time for v0.5.9+ runs).  For older runs
    # that have no sidecar, inspect the existing event log — if any event has a
    # non-empty signature, the run was signed and the lifecycle event must be
    # signed too.  Never silently downgrade a signed run to unsigned.
    signing_key_path = ev_cfg["signing_key_path"]
    if signing_key_path is None:
        existing = events.read_all()
        if any(e.signature for e in existing):
            raise EvidenceConfigError(
                "run has signed events but no evidence_config sidecar — cannot "
                "determine signing key for lifecycle event. Re-run with v0.5.9+ "
                "or manually create evidence_config.json with the signing_key_path."
            )

    if signing_key_path is not None:
        try:
            key_bytes = Path(signing_key_path).read_bytes()
        except OSError as exc:
            raise EvidenceConfigError(
                f"run was signed but signing key is not readable: {signing_key_path} ({exc})"
            ) from exc
        if len(key_bytes) != 32:
            raise EvidenceConfigError(
                f"signing key file must be exactly 32 bytes, got {len(key_bytes)}"
            )
        try:
            events.set_signing_key(key_bytes)
        except ImportError as exc:
            raise EvidenceConfigError(
                "run was signed but 'cryptography' is not installed — cannot sign "
                "lifecycle event. Install with: uv sync --extra crypto"
            ) from exc

    # --- Full evidence checkpoint ------------------------------------------ #
    # Save the state of every evidence file that could be modified during the
    # lifecycle transaction.  On failure, we restore ALL of them — not just
    # events.jsonl.  This is what makes the lifecycle write truly transactional.
    report_path = run_dir / "artifacts" / "final_report.md"
    lifecycle_manifest_path = run_dir / "lifecycle_manifest.json"

    event_checkpoint = events.checkpoint()
    report_backup = report_path.read_bytes() if report_path.is_file() else None
    lifecycle_manifest_backup = (
        lifecycle_manifest_path.read_bytes() if lifecycle_manifest_path.is_file() else None
    )

    durable_store_path = ev_cfg["durable_store"]
    durable_mode = ev_cfg.get("durable_mode")
    durable_warning = None

    # For the durable store, we use a single SQLite transaction spanning both
    # the lifecycle event and the report_bound event.  This way, if the second
    # insert fails, the first is automatically rolled back by SQLite — no
    # orphan human.approved in the DB.
    durable_db = None
    durable_tx_active = False

    try:
        # Open the durable store connection once and begin a transaction.
        if durable_store_path is not None:
            from acp.evidence.durable_store import DurableEventStore

            durable_db = DurableEventStore(durable_store_path)
            durable_db.init()
            if durable_mode == "required":
                # Use a single explicit transaction for both events.
                durable_db.begin_transaction()
                durable_tx_active = True

        # v0.7.4: In durable_mode="required", write to SQLite FIRST, then
        # to JSONL. The previous order (JSONL → SQLite) required a
        # file truncation rollback (f.truncate) if the SQLite write failed,
        # which is not crash-safe — a power failure during the rollback
        # corrupts events.jsonl. By committing the SQLite transaction
        # first, we ensure the durable store is the authoritative write;
        # the JSONL append is then a best-effort mirror that can be
        # rebuilt from SQLite via rebuild_from_jsonl if it fails.
        #
        # In non-required mode, the order doesn't matter (both are
        # best-effort), so we keep the original JSONL-first order for
        # backwards compatibility.

        if durable_tx_active:
            # --- Required mode: SQLite first, then JSONL ------------------- #
            assert durable_db is not None  # for mypy: durable_tx_active implies durable_db
            # 1a. Build the event object (without writing to JSONL yet).
            evt = events.build_event(event_type, payload)

            # 1b. Write to the durable store (within the explicit transaction).
            durable_db.append(evt)  # within the transaction

            # 1c. Now append to JSONL (the SQLite write succeeded).
            events.append_event(evt)

            # 3. Re-render the report (this overwrites final_report.md).
            try:
                from acp.reports.writer import rerender_report_from_run

                rerender_report_from_run(run_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "report re-render failed during lifecycle event: "
                    "%s — final_report.md may be stale",
                    exc,
                )

            # 4. Build the report_bound event (without writing to JSONL yet).
            report_hash = compute_report_hash(run_dir)
            report_bound_evt = None
            if report_hash is not None:
                report_bound_evt = events.build_event(
                    EventType.EVIDENCE_REPORT_BOUND,
                    {
                        "task_id": task_id,
                        "report_hash": report_hash,
                        "lifecycle_event": event_type.value,
                        "event_chain_head_before_report_bound": events.last_hash,
                    },
                )
                # Write to durable store first.
                durable_db.append(report_bound_evt)
                # Then append to JSONL.
                events.append_event(report_bound_evt)

            # 6. Commit the durable transaction (both events at once).
            durable_db.commit()
            durable_tx_active = False

        else:
            # --- Non-required mode: JSONL first (backwards compatible) ----- #
            # 1. Write the lifecycle event to events.jsonl.
            evt = events.write(event_type, payload)

            # 2. Write the lifecycle event to the durable store.
            if durable_db is not None:
                try:
                    durable_db.append(evt)
                    durable_db.commit()
                except Exception as exc:
                    durable_warning = f"durable store write failed: {exc}"

            # 3. Re-render the report (this overwrites final_report.md).
            try:
                from acp.reports.writer import rerender_report_from_run

                rerender_report_from_run(run_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "report re-render failed during lifecycle event: "
                    "%s — final_report.md may be stale",
                    exc,
                )

            # 4. Write the evidence.report_bound event to events.jsonl.
            report_hash = compute_report_hash(run_dir)
            report_bound_evt = None
            if report_hash is not None:
                report_bound_evt = events.write(
                    EventType.EVIDENCE_REPORT_BOUND,
                    {
                        "task_id": task_id,
                        "report_hash": report_hash,
                        "lifecycle_event": event_type.value,
                        "event_chain_head_before_report_bound": events.last_hash,
                    },
                )

            # 5. Write the report_bound event to the durable store.
            if report_bound_evt is not None and durable_db is not None:
                try:
                    durable_db.append(report_bound_evt)
                    durable_db.commit()
                except Exception as exc:
                    durable_warning = f"durable store write failed: {exc}"

        # 7. Write the lifecycle manifest (best-effort).
        try:
            write_lifecycle_manifest(run_dir=run_dir, events_writer=events, report_hash=report_hash)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lifecycle manifest write failed: %s — evidence manifest may be stale",
                exc,
            )

    except Exception as rollback_exc:
        # v0.7.4: Log the exception that triggered the rollback at ERROR
        # level. Previously this was a bare `except Exception:` that
        # silently swallowed the root cause, making it impossible to
        # debug why lifecycle transactions were failing.
        logger.error("lifecycle transaction failed, rolling back: %s", rollback_exc)
        # --- Full rollback: restore ALL evidence files --------------------- #
        # 1. Restore events.jsonl (truncation + hash chain restore).
        events.rollback(event_checkpoint)

        # 2. Restore final_report.md.
        if report_backup is not None:
            report_path.write_bytes(report_backup)
        elif report_path.is_file():
            report_path.unlink()

        # 3. Restore lifecycle_manifest.json.
        if lifecycle_manifest_backup is not None:
            lifecycle_manifest_path.write_bytes(lifecycle_manifest_backup)
        elif lifecycle_manifest_path.is_file():
            lifecycle_manifest_path.unlink()

        # 4. Roll back the SQLite transaction (undoes both inserts).
        if durable_tx_active and durable_db is not None:
            try:
                durable_db.rollback_transaction()
            except Exception as exc:  # noqa: BLE001
                logger.warning("durable store rollback failed: %s", exc)

        # 5. Close the durable connection.
        if durable_db is not None:
            try:
                durable_db.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("durable store close failed: %s", exc)

        if durable_mode == "required":
            raise ACPError(
                "lifecycle event aborted — all evidence restored to pre-lifecycle "
                "state. Durable store write failed (mode=required).",
                exit_code=1,
            )
        raise

    # Close the durable connection on success.
    if durable_db is not None:
        try:
            durable_db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("durable store close failed after success: %s", exc)

    return durable_warning
