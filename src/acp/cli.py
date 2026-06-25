"""ACP command-line interface.

``acp run`` is the entry point: it runs one coding task in an isolated git
worktree, captures everything, reviews the diff, and writes an evidence
report + Obsidian note. The orchestration lives in the LangGraph workflow
(``acp.graph.workflow``) — the graph is the only production engine. The
original linear ``EvidenceLoop`` is quarantined in ``acp.legacy_loop`` for
test-only equivalence checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from acp.config import load_repo_config
from acp.errors import ACPError
from acp.events import EventWriter
from acp.models import EventType, Task, TaskStatus
from acp.store import TaskStore, is_valid_task_id

app = typer.Typer(
    name="acp",
    help="agent-control-plane: safely run one coding task, capture everything.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# --------------------------------------------------------------------------- #
# task_id validation — every command that turns a user-supplied task id into a
# filesystem path must reject anything that isn't ``task_<YYYYMMDD>_<NNNN>``.
# A local control plane that manipulates files must not accept path-shaped ids.
# --------------------------------------------------------------------------- #

def _require_valid_task_id(task_id: str) -> None:
    """Exit nonzero if ``task_id`` is not a canonical task id."""
    if not is_valid_task_id(task_id):
        console.print(
            f"[red]✗[/] invalid task id: {task_id!r} "
            f"(expected task_<YYYYMMDD>_<NNNN>, e.g. task_20260624_0001)"
        )
        raise typer.Exit(code=1)


def _revert_vault_note(note_path: Path, original_content: str) -> None:
    """Best-effort revert of a vault note to its pre-modification content.

    Deprecated in v0.5.14: with pure-projection re-rendering, the vault note
    is no longer modified before the event log is written, so this function
    is only kept for backward compatibility with any external callers.
    """
    try:
        note_path.write_text(original_content)
    except Exception:  # noqa: BLE001
        console.print(f"[red]✗[/] could not revert vault note: {note_path}")


def _rerender_vault_note_from_state(
    *,
    note_path: Path,
    run_dir: Path,
    task: Any,
    store: TaskStore,
    vault_root: Path,
) -> None:
    """Re-render a vault note from scratch as a pure projection of state.

    v0.5.14: Replaces the old in-place editing + body-update approach.
    Reads the current event log, report, and task state, then rebuilds
    the vault note entirely. The note's frontmatter (approved, memory_status,
    audit_trail) is derived from the event log, not modified in-place.

    Best-effort: if re-rendering fails, the vault note keeps its current
    content. The event log is authoritative; the note is a human convenience.
    """
    try:
        from acp.events import EventWriter
        from acp.gitops.diff import DiffCapture
        from acp.models import ReviewResult
        from acp.vault.obsidian_writer import rerender_vault_note

        # Read the current report.
        report_path = run_dir / "artifacts" / "final_report.md"
        report_body = report_path.read_text() if report_path.is_file() else ""

        # Read the current event log.
        events_writer = EventWriter(task.task_id, run_dir)
        events = events_writer.read_all()

        # Read the review result if it exists.
        review_path = run_dir / "artifacts" / "review.json"
        review = ReviewResult(risk="low", recommendation="merge")
        if review_path.is_file():
            import json
            try:
                review = ReviewResult.model_validate_json(review_path.read_text())
            except Exception:  # noqa: BLE001
                pass

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
            except Exception:  # noqa: BLE001
                pass

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
        console.print(f"[yellow]![/] vault note re-render failed: {exc}")
        console.print(f"    event log is authoritative; note may be stale")


# --------------------------------------------------------------------------- #
# Post-run lifecycle evidence — used by ``acp approve`` / ``acp reject``.
#
# Approval/rejection are human decisions that happen *after* a run's terminal
# event. They append a lifecycle event (human.approved / human.rejected) to the
# same hash-chained event log. To keep ``acp verify`` passing, the lifecycle
# event must be:
#   1. signed with the run's own Ed25519 key (recovered from the
#      evidence_config sidecar) — a signed run's lifecycle event must not be
#      unsigned (fail closed);
#   2. dual-written to the run's SQLite durable store, if it had one;
#   3. followed by an evidence manifest recompute, so the manifest's event
#      chain head matches the new last event.
# --------------------------------------------------------------------------- #

def _record_lifecycle_event(
    *,
    task_id: str,
    run_dir: Path,
    event_type: EventType,
    payload: dict,
) -> str | None:
    """Append a post-run lifecycle event with full transactional integrity.

    All evidence mutations — events.jsonl, final_report.md,
    lifecycle_manifest.json, and the SQLite durable store — are staged
    and committed as one transaction. If ANY step fails (especially a
    required-mode durable store write), ALL evidence files are restored
    to their pre-lifecycle state. No partial state survives.

    Returns a warning string if the durable store write failed in
    best_effort mode, or ``None`` if everything succeeded. In required
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

    # Determine whether the run was signed. The evidence_config sidecar is the
    # primary signal (written at finalize time for v0.5.9+ runs). For older runs
    # that have no sidecar, inspect the existing event log — if any event has a
    # non-empty signature, the run was signed and the lifecycle event must be
    # signed too. Never silently downgrade a signed run to unsigned.
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
            key_bytes = signing_key_path.read_bytes()
        except OSError as exc:
            raise EvidenceConfigError(
                f"run was signed but signing key is not readable: "
                f"{signing_key_path} ({exc})"
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
    # lifecycle transaction. On failure, we restore ALL of them — not just
    # events.jsonl. This is what makes the lifecycle write truly transactional.
    report_path = run_dir / "artifacts" / "final_report.md"
    lifecycle_manifest_path = run_dir / "lifecycle_manifest.json"

    event_checkpoint = events.checkpoint()
    report_backup = report_path.read_bytes() if report_path.is_file() else None
    lifecycle_manifest_backup = (
        lifecycle_manifest_path.read_bytes()
        if lifecycle_manifest_path.is_file()
        else None
    )

    durable_store_path = ev_cfg["durable_store"]
    durable_mode = ev_cfg.get("durable_mode")
    durable_warning = None

    # For the durable store, we use a single SQLite transaction spanning both
    # the lifecycle event and the report_bound event. This way, if the second
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

        # 1. Write the lifecycle event to events.jsonl.
        evt = events.write(event_type, payload)

        # 2. Write the lifecycle event to the durable store.
        if durable_db is not None:
            try:
                if durable_tx_active:
                    durable_db.append(evt)  # within the explicit transaction
                else:
                    durable_db.append(evt)
                    durable_db.commit()
            except Exception as exc:
                if durable_mode == "required":
                    raise  # triggers full rollback below
                durable_warning = f"durable store write failed: {exc}"

        # 3. Re-render the report (this overwrites final_report.md).
        try:
            from acp.reports.writer import rerender_report_from_run
            rerender_report_from_run(run_dir)
        except Exception:  # noqa: BLE001
            pass  # report re-render is best-effort

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
                if durable_tx_active:
                    durable_db.append(report_bound_evt)
                else:
                    durable_db.append(report_bound_evt)
                    durable_db.commit()
            except Exception as exc:
                if durable_mode == "required":
                    raise  # triggers full rollback below
                durable_warning = f"durable store write failed: {exc}"

        # 6. Commit the durable transaction (both events at once).
        if durable_tx_active:
            try:
                durable_db.commit()
                durable_tx_active = False
            except Exception as exc:
                if durable_mode == "required":
                    raise  # triggers full rollback
                durable_warning = f"durable store commit failed: {exc}"

        # 7. Write the lifecycle manifest (best-effort).
        try:
            write_lifecycle_manifest(
                run_dir=run_dir, events_writer=events, report_hash=report_hash
            )
        except Exception:  # noqa: BLE001
            pass

    except Exception:
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
            except Exception:  # noqa: BLE001
                pass  # best-effort; connection close will also undo

        # 5. Close the durable connection.
        if durable_db is not None:
            try:
                durable_db.close()
            except Exception:  # noqa: BLE001
                pass

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
        except Exception:  # noqa: BLE001
            pass

    return durable_warning


# --------------------------------------------------------------------------- #
# Typer commands
# --------------------------------------------------------------------------- #

@app.command()
def run(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    task: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="The coding task to perform.",
    ),
    vault: Path = typer.Option(
        Path("vault"),
        "--vault",
        help="Obsidian vault root (default: ./vault).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
) -> None:
    """Run one coding task in an isolated worktree and write an evidence report."""
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold]ACP run[/] · repo={cfg.repo.name} · "
        f"agent={cfg.agent.default} · task={task!r}"
    )
    try:
        from acp.graph.workflow import run_workflow
        result = run_workflow(
            config=cfg,
            user_request=task,
            runs_root=runs_root,
            vault_root=vault,
        )
        status = result.get("status")
        report_path = result.get("report_path")
        vault_note_path = result.get("vault_note_path")

        # Surface durable-store warnings to the operator.
        durable_warnings = result.get("durable_store_warnings")
        if durable_warnings:
            for w in durable_warnings:
                console.print(f"[yellow]![/] durable store: {w}")

        # Print output that reflects the actual outcome — no green checkmarks
        # on failures or missing evidence. A failed run must look failed.
        if status == TaskStatus.PASSED:
            console.print(f"[green]✓[/] report: {report_path or '(missing)'}")
            console.print(f"[green]✓[/] vault: {vault_note_path or '(missing)'}")
            console.print(
                f"\n[dim]Task {result.get('task_id')} → passed. "
                f"Review the vault note and set approved: true to promote memory.[/]"
            )
        elif status == TaskStatus.NEEDS_REVIEW:
            console.print(f"[yellow]![/] report: {report_path or '(missing)'}")
            console.print(f"[yellow]![/] vault: {vault_note_path or '(missing)'}")
            console.print(
                f"\n[dim]Task {result.get('task_id')} → needs review. "
                f"Review the vault note and set approved: true to promote memory.[/]"
            )
        else:
            console.print(f"[red]✗[/] report: {report_path or '(missing)'}")
            console.print(f"[red]✗[/] vault: {vault_note_path or '(missing)'}")
            error = result.get("error", "unknown")
            console.print(
                f"\n[dim]Task {result.get('task_id')} → failed: {error}.[/]"
            )
            raise typer.Exit(code=1)
    except ACPError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


@app.command()
def version() -> None:
    """Print the ACP version."""
    from acp import __version__
    console.print(f"agent-control-plane {__version__}")


@app.command()
def verify(
    task_id: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Task id to verify (e.g. task_20260624_0001).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    public_key: Path = typer.Option(
        None,
        "--public-key",
        help="Path to a 32-byte raw Ed25519 public key for signature verification.",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Deep mode: recompute all declared artifact hashes from disk (slower, stricter).",
    ),
    check_durable: bool = typer.Option(
        False,
        "--check-durable",
        help="Check that the SQLite durable store contains the same events as events.jsonl. "
             "Automatically enabled when durable_mode=required.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Show full Python tracebacks on errors (for developers).",
    ),
) -> None:
    """Verify the evidence integrity of a completed run.

    Checks:
      1. Task identity binding — CLI task_id == task.json.task_id ==
         manifest.task_id == every event.task_id == directory name
      2. Event hash chain is valid (tamper-evident)
      3. Evidence manifest exists, manifest_hash is correct, and all artifact
         hashes match
      4. evidence.finalized event binds artifacts + task.json to signed event log
      5. evidence.report_bound event binds the human-facing report
      6. Lifecycle manifest required + valid when lifecycle events exist
      7. (Optional) Ed25519 event signatures are valid

    Default (fast) mode checks event chain, signatures, manifest structure,
    task identity, and selected evidence hashes. ``--deep`` mode recomputes
    all declared artifact hashes from disk — use in CI/nightly for stricter
    verification.

    Exits 0 if all checks pass, 1 if any fail. Malformed data produces clean
    error messages, not tracebacks. Use ``--debug`` to see full tracebacks.
    """
    try:
        _verify_impl(task_id, runs_root, public_key, deep, check_durable, debug)
    except typer.Exit:
        raise
    except Exception as exc:
        if debug:
            raise
        console.print(f"[red]✗[/] verification error: {exc}")
        raise typer.Exit(code=1) from exc


def _verify_impl(
    task_id: str,
    runs_root: Path,
    public_key: Path | None,
    deep: bool,
    check_durable: bool,
    debug: bool,
) -> None:
    """Internal verify implementation — separated for clean error handling."""
    from acp.events import verify_event_chain, verify_event_signatures
    from acp.evidence.manifest import verify_evidence_manifest
    from acp.models import Event, EventType

    _require_valid_task_id(task_id)
    store = TaskStore(runs_root=runs_root)
    run_dir = store.run_dir(task_id)

    if not run_dir.is_dir():
        console.print(f"[red]✗[/] run directory not found: {run_dir}")
        raise typer.Exit(code=1)

    all_ok = True
    events: list[Event] = []
    has_malformed_events = False

    # 0. Task identity binding — all identity fields must agree.
    dir_name = run_dir.name
    if dir_name != task_id:
        console.print(f"[red]✗[/] task ID mismatch: CLI='{task_id}' vs dir='{dir_name}'")
        all_ok = False

    # task.json task_id
    task_json_path = run_dir / "task.json"
    task_json_id: str | None = None
    if task_json_path.is_file():
        try:
            import json as _json
            task_json_id = _json.loads(task_json_path.read_text()).get("task_id")
        except Exception:
            console.print(f"[red]✗[/] task.json is malformed — cannot read task_id")
            all_ok = False
    else:
        console.print(f"[yellow]![/] task.json not found — cannot verify task_id binding")
        all_ok = False
    if task_json_id is not None and task_json_id != task_id:
        console.print(f"[red]✗[/] task ID mismatch: CLI='{task_id}' vs task.json='{task_json_id}'")
        all_ok = False

    # 1. Event hash chain — with clean error handling for malformed lines.
    events_path = store.events_path(task_id)
    if not events_path.is_file():
        console.print(f"[red]✗[/] event log not found: {events_path}")
        all_ok = False
    else:
        lines = events_path.read_text().splitlines()
        events = []
        malformed_lines: list[int] = []
        for i, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                events.append(Event.model_validate_json(line))
            except Exception:
                malformed_lines.append(i)
        if malformed_lines:
            console.print(
                f"[red]✗[/] event log malformed at line(s): {', '.join(str(n) for n in malformed_lines[:5])}"
            )
            all_ok = False
            has_malformed_events = True
        elif not events:
            console.print(f"[red]✗[/] event log is empty")
            all_ok = False
        elif verify_event_chain(events):
            console.print(f"[green]✓[/] event chain valid ({len(events)} events, head={events[-1].hash[:16]}...)")
        else:
            console.print(f"[red]✗[/] event chain INVALID — log has been tampered with")
            all_ok = False

        # Check every event's task_id matches the CLI task_id.
        if events:
            mismatched = [e for e in events if e.task_id != task_id]
            if mismatched:
                console.print(
                    f"[red]✗[/] task ID mismatch: {len(mismatched)} event(s) have "
                    f"task_id != '{task_id}' (e.g. '{mismatched[0].task_id}')"
                )
                all_ok = False

    # Detect lifecycle events — these require a lifecycle manifest.
    lifecycle_types = {"human.approved", "human.rejected", "memory.promoted"}
    has_lifecycle = any(e.type.value in lifecycle_types for e in events)
    has_evidence_finalized = any(e.type == EventType.EVIDENCE_FINALIZED for e in events)

    # 1b. task.json.status consistency — the event log is truth; task.json
    # is only a projection. If task.json.status doesn't match the status
    # derived from the event log, the projection is stale or tampered.
    if events and task_json_path.is_file():
        from acp.evidence.manifest import derive_status_from_events
        try:
            import json as _json_status
            task_data = _json_status.loads(task_json_path.read_text())
            task_json_status = task_data.get("status")
            expected_status = derive_status_from_events(events)
            if expected_status is not None and task_json_status != expected_status:
                console.print(
                    f"[red]✗[/] task.json status inconsistent: task.json='{task_json_status}' "
                    f"vs event log='{expected_status}'. The event log is truth; "
                    f"task.json is a projection and must match."
                )
                all_ok = False
        except Exception:
            pass  # malformed task.json already reported above

    # 2. Evidence manifest — required for v0.5.10+ runs (those with
    # evidence.finalized). Missing manifest is fatal, not a warning.
    manifest_path = run_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        if has_evidence_finalized:
            # v0.5.10+ run — the manifest is required evidence. Its absence
            # means the evidence set has been tampered with.
            console.print(f"[red]✗[/] evidence manifest not found — required for runs with evidence.finalized")
            all_ok = False
        else:
            console.print(f"[yellow]![/] evidence manifest not found (runs before v0.5.5 don't have one)")
    else:
        try:
            import json as _json
            manifest = _json.loads(manifest_path.read_text())
            manifest_task_id = manifest.get("task_id")
            if manifest_task_id is not None and manifest_task_id != task_id:
                console.print(
                    f"[red]✗[/] task ID mismatch: CLI='{task_id}' vs manifest='{manifest_task_id}'"
                )
                all_ok = False
        except Exception:
            console.print(f"[red]✗[/] evidence manifest is malformed JSON")
            all_ok = False

        if verify_evidence_manifest(run_dir, deep=deep):
            mode_label = "deep" if deep else "fast"
            console.print(
                f"[green]✓[/] evidence manifest valid ({mode_label} mode: "
                f"artifacts + report + task.json + event chain + manifest_hash match)"
            )
        else:
            console.print(
                f"[red]✗[/] evidence manifest INVALID — artifacts, report, task.json, "
                f"event log, or manifest_hash don't match"
            )
            all_ok = False

    # 3. Ed25519 signatures (optional). If the event log is malformed, we
    # skip the "signatures valid" success message — printing it would be
    # misleading, since it only applies to the parsed subset, not the full log.
    if public_key is not None:
        if not events_path.is_file():
            console.print(f"[red]✗[/] cannot verify signatures without event log")
            all_ok = False
        elif has_malformed_events:
            console.print(
                f"[red]✗[/] signature verification skipped because event log is malformed"
            )
            all_ok = False
        else:
            try:
                pk_bytes = public_key.read_bytes()
                if len(pk_bytes) != 32:
                    console.print(f"[red]✗[/] public key must be exactly 32 bytes, got {len(pk_bytes)}")
                    all_ok = False
                elif not events:
                    console.print(f"[red]✗[/] cannot verify signatures on empty event log")
                    all_ok = False
                elif verify_event_signatures(events, pk_bytes):
                    console.print(f"[green]✓[/] Ed25519 signatures valid ({len(events)} events signed)")
                else:
                    console.print(f"[red]✗[/] Ed25519 signature verification FAILED")
                    all_ok = False
            except ImportError:
                console.print(f"[yellow]![/] cryptography package not installed — install with: uv sync --extra crypto")
            except Exception as exc:
                console.print(f"[red]✗[/] signature verification error: {exc}")
                all_ok = False

    # 4. Lifecycle manifest — required when lifecycle events exist. A missing
    # lifecycle manifest with lifecycle events in the log means the lifecycle
    # record has been deleted (tampering).
    from acp.evidence.manifest import verify_lifecycle_manifest
    lifecycle_path = run_dir / "lifecycle_manifest.json"
    if has_lifecycle:
        if not lifecycle_path.is_file():
            console.print(
                f"[red]✗[/] lifecycle manifest not found — required when lifecycle "
                f"events (approve/reject/promote) exist in the event log"
            )
            all_ok = False
        elif verify_lifecycle_manifest(run_dir):
            console.print(f"[green]✓[/] lifecycle manifest valid")
        else:
            console.print(f"[red]✗[/] lifecycle manifest INVALID — lifecycle events don't match")
            all_ok = False
    elif lifecycle_path.is_file():
        # Lifecycle manifest exists but no lifecycle events — verify it's valid.
        if verify_lifecycle_manifest(run_dir):
            console.print(f"[green]✓[/] lifecycle manifest valid")
        else:
            console.print(f"[red]✗[/] lifecycle manifest INVALID — lifecycle events don't match")
            all_ok = False

    # Report final approval state if present.
    if events:
        from acp.models import EventType as _ET
        approved = any(e.type == _ET.HUMAN_APPROVED for e in events)
        rejected = any(e.type == _ET.HUMAN_REJECTED for e in events)
        if approved:
            console.print(f"[dim]Final approval state: approved[/]")
        elif rejected:
            console.print(f"[dim]Final approval state: rejected[/]")

    # 5. Durable store consistency check — when --check-durable is passed
    # or when durable_mode=required, verify that the SQLite store contains
    # the same events as events.jsonl. This catches orphan events from
    # failed lifecycle writes and SQLite/JSONL divergence.
    from acp.evidence.manifest import read_evidence_config
    ev_cfg = read_evidence_config(run_dir)
    durable_store_path = ev_cfg.get("durable_store")
    durable_mode = ev_cfg.get("durable_mode")
    should_check_durable = check_durable or durable_mode == "required"
    if should_check_durable and durable_store_path is not None and events:
        try:
            from acp.evidence.durable_store import DurableEventStore
            with DurableEventStore(durable_store_path) as db:
                db_events = db.query(task_id=task_id, limit=10000)
                if len(db_events) == len(events):
                    # Check that every event in the JSONL log is in SQLite.
                    jsonl_hashes = {e.hash for e in events}
                    db_hashes = {e.hash for e in db_events}
                    if jsonl_hashes == db_hashes:
                        console.print(
                            f"[green]✓[/] durable store consistent "
                            f"({len(db_events)} events match events.jsonl)"
                        )
                    else:
                        missing = jsonl_hashes - db_hashes
                        extra = db_hashes - jsonl_hashes
                        console.print(
                            f"[red]✗[/] durable store inconsistent: "
                            f"{len(missing)} event(s) missing from SQLite, "
                            f"{len(extra)} orphan event(s) in SQLite"
                        )
                        all_ok = False
                else:
                    console.print(
                        f"[red]✗[/] durable store inconsistent: "
                        f"events.jsonl has {len(events)} events, "
                        f"SQLite has {len(db_events)}"
                    )
                    all_ok = False
        except Exception as exc:
            console.print(f"[red]✗[/] durable store check failed: {exc}")
            all_ok = False

    if all_ok:
        console.print(f"\n[green]✓ All evidence checks passed for task {task_id}.[/]")
    else:
        console.print(f"\n[red]✗ Evidence verification FAILED for task {task_id}.[/]")
        raise typer.Exit(code=1)


@app.command()
def events(
    task_id: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Task id to list events for (e.g. task_20260624_0001).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    filter_type: str = typer.Option(
        None,
        "--type",
        help="Filter by event type (e.g. task.failed, repair.attempted).",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        help="Maximum number of events to show (0 = all).",
    ),
) -> None:
    """List events from a completed run's event log.

    Shows the event timeline with id, type, timestamp, and hash prefix.
    Use --type to filter by event type, --limit to cap the output.
    """
    from acp.models import Event

    _require_valid_task_id(task_id)
    store = TaskStore(runs_root=runs_root)
    events_path = store.events_path(task_id)

    if not events_path.is_file():
        console.print(f"[red]✗[/] event log not found: {events_path}")
        raise typer.Exit(code=1)

    all_events = [
        Event.model_validate_json(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]

    if filter_type:
        valid_types = {t.value for t in EventType}
        if filter_type not in valid_types:
            console.print(
                f"[red]✗[/] unknown event type: {filter_type!r} "
                f"(valid types: {', '.join(sorted(valid_types))})"
            )
            raise typer.Exit(code=1)
        all_events = [e for e in all_events if e.type.value == filter_type]

    if limit > 0:
        all_events = all_events[:limit]

    if not all_events:
        console.print(f"[dim]No events found for task {task_id}.[/]")
        return

    console.print(f"[bold]Events for task {task_id}[/] ({len(all_events)} total):\n")
    console.print(f"  {'#':>3}  {'event_id':<14}  {'type':<24}  {'timestamp':<22}  {'hash (first 12)'}")
    console.print(f"  {'---':>3}  {'---':<14}  {'---':<24}  {'---':<22}  {'---'}")
    for i, evt in enumerate(all_events):
        short_hash = evt.hash[:12] if evt.hash else "—"
        signed = " ✓" if evt.signature else ""
        console.print(
            f"  {i + 1:>3}  {evt.event_id:<14}  {evt.type.value:<24}  {evt.timestamp:<22}  {short_hash}{signed}"
        )


@app.command()
def approve(
    task_id: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Task id to approve (e.g. task_20260624_0001).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    vault_root: Path = typer.Option(
        Path("vault"),
        "--vault-root",
        help="Root directory of the Obsidian vault (default: vault).",
    ),
    approver: str = typer.Option(
        "",
        "--approver",
        help="Name/email of the person approving (recorded in the event log).",
    ),
) -> None:
    """Approve a task's vault note — the human decision that gates memory.

    This is the single most important safety property in ACP: the system
    cannot gaslight itself, because every fact it remembers was first read
    and approved by a human. This command:

      1. Writes a ``human.approved`` event to the event log (source of truth)
      2. Re-renders the vault note from scratch as a pure projection
      3. Updates the task status to ``APPROVED``

    v0.5.14: The vault note is no longer modified in-place. The event log is
    written first; if that fails, the vault note is untouched (no revert
    needed). The note is then re-rendered from the event log + report.

    Only ``PASSED`` and ``NEEDS_REVIEW`` tasks can be approved. Already-approved
    notes cannot be approved again.
    """
    from acp.vault.approval import can_approve
    from acp.vault.obsidian_writer import rerender_vault_note

    _require_valid_task_id(task_id)
    store = TaskStore(runs_root=runs_root)
    run_dir = store.run_dir(task_id)

    if not run_dir.is_dir():
        console.print(f"[red]✗[/] run directory not found: {run_dir}")
        raise typer.Exit(code=1)

    # Load the task to check eligibility.
    try:
        task = store.load(task_id)
    except Exception as exc:
        console.print(f"[red]✗[/] cannot load task.json: {exc}")
        raise typer.Exit(code=1)

    if not can_approve(task.status):
        console.print(
            f"[red]✗[/] task status is '{task.status.value}' — "
            f"only 'passed' or 'needs_review' tasks can be approved."
        )
        raise typer.Exit(code=1)

    # Find the vault note.
    note_path = vault_root / "tasks" / f"{task_id}.md"
    if not note_path.is_file():
        console.print(f"[red]✗[/] vault note not found: {note_path}")
        raise typer.Exit(code=1)

    # v0.5.14 pure projection: write the lifecycle event FIRST (source of
    # truth), then re-render the vault note from scratch. If the event write
    # fails, the vault note is untouched — no revert needed.
    try:
        durable_warning = _record_lifecycle_event(
            task_id=task_id,
            run_dir=run_dir,
            event_type=EventType.HUMAN_APPROVED,
            payload={
                "approver": approver or "unknown",
                "vault_note_path": str(note_path),
            },
        )
    except ACPError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        console.print(f"[red]✗[/] lifecycle event write failed: {exc}")
        raise typer.Exit(code=1) from exc

    if durable_warning:
        console.print(f"[yellow]![/] {durable_warning}")

    # Update task status to APPROVED before re-rendering the vault note.
    task.status = TaskStatus.APPROVED
    store.save(task)

    # Re-render the vault note from scratch as a pure projection of the
    # current state (event log + report + task). The note's frontmatter
    # (approved=true, memory_status=active, audit_trail) is derived from
    # the event log, not modified in-place.
    _rerender_vault_note_from_state(
        note_path=note_path,
        run_dir=run_dir,
        task=task,
        store=store,
        vault_root=vault_root,
    )

    console.print(f"[green]✓[/] task {task_id} approved by {approver or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print(f"  memory_status: active")
    console.print(f"  event: human.approved written to {store.events_path(task_id)}")
    console.print(f"  lifecycle evidence written — `acp verify` remains valid")


@app.command()
def reject(
    task_id: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Task id to reject (e.g. task_20260624_0001).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    vault_root: Path = typer.Option(
        Path("vault"),
        "--vault-root",
        help="Root directory of the Obsidian vault (default: vault).",
    ),
    rejecter: str = typer.Option(
        "",
        "--rejecter",
        help="Name/email of the person rejecting (recorded in the event log).",
    ),
    reason: str = typer.Option(
        "",
        "--reason",
        help="Optional reason for rejection (recorded in the event log).",
    ),
) -> None:
    """Reject a task's vault note — archive it so it can never be promoted.

    Sets ``memory_status: archived`` and writes a ``human.rejected`` event.
    The task status is updated to ``ARCHIVED``. Cannot reject an already-
    approved note (approval is a commitment).

    v0.5.14: The vault note is no longer modified in-place. The event log is
    written first; if that fails, the vault note is untouched. The note is
    then re-rendered from the event log + report.
    """
    from acp.vault.approval import can_approve
    from acp.vault.obsidian_writer import rerender_vault_note

    _require_valid_task_id(task_id)
    store = TaskStore(runs_root=runs_root)
    run_dir = store.run_dir(task_id)

    if not run_dir.is_dir():
        console.print(f"[red]✗[/] run directory not found: {run_dir}")
        raise typer.Exit(code=1)

    try:
        task = store.load(task_id)
    except Exception as exc:
        console.print(f"[red]✗[/] cannot load task.json: {exc}")
        raise typer.Exit(code=1)

    # Cannot reject an already-approved, already-rejected, or archived task.
    if task.status == TaskStatus.APPROVED:
        console.print(
            f"[red]✗[/] task is already approved — cannot reject after approval."
        )
        raise typer.Exit(code=1)
    if task.status == TaskStatus.REJECTED:
        console.print(
            f"[red]✗[/] task is already rejected — cannot reject again."
        )
        raise typer.Exit(code=1)
    if task.status == TaskStatus.ARCHIVED:
        console.print(
            f"[red]✗[/] task is already archived — cannot reject again."
        )
        raise typer.Exit(code=1)

    note_path = vault_root / "tasks" / f"{task_id}.md"
    if not note_path.is_file():
        console.print(f"[red]✗[/] vault note not found: {note_path}")
        raise typer.Exit(code=1)

    # v0.5.14 pure projection: write the lifecycle event FIRST, then
    # re-render the vault note. No in-place modification, no revert.
    try:
        durable_warning = _record_lifecycle_event(
            task_id=task_id,
            run_dir=run_dir,
            event_type=EventType.HUMAN_REJECTED,
            payload={
                "rejecter": rejecter or "unknown",
                "reason": reason,
                "vault_note_path": str(note_path),
            },
        )
    except ACPError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        console.print(f"[red]✗[/] lifecycle event write failed: {exc}")
        raise typer.Exit(code=1) from exc

    if durable_warning:
        console.print(f"[yellow]![/] {durable_warning}")

    task.status = TaskStatus.REJECTED
    store.save(task)

    # Re-render the vault note from scratch as a pure projection.
    _rerender_vault_note_from_state(
        note_path=note_path,
        run_dir=run_dir,
        task=task,
        store=store,
        vault_root=vault_root,
    )

    console.print(f"[green]✓[/] task {task_id} rejected by {rejecter or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print(f"  memory_status: archived")
    console.print(f"  event: human.rejected written to {store.events_path(task_id)}")
    console.print(f"  lifecycle evidence written — `acp verify` remains valid")


@app.command("list")
def list_tasks(
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    status: str = typer.Option(
        None,
        "--status",
        "-s",
        help="Filter by task status (e.g. passed, failed, needs_review, approved, archived).",
    ),
) -> None:
    """List tasks from the run directory.

    Shows task id, status, repo, and created timestamp. Use --status to
    filter by a specific status.
    """
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        console.print(f"[dim]No runs directory found at {runs_root}[/]")
        return

    tasks: list[tuple[str, str, str, str]] = []
    skipped = 0
    for task_json in sorted(runs_root.rglob("task.json")):
        try:
            t = Task.model_validate_json(task_json.read_text())
            if status and t.status.value != status:
                continue
            tasks.append((t.task_id, t.status.value, t.repo_name, t.created_at))
        except Exception:  # noqa: BLE001
            skipped += 1

    if skipped:
        console.print(f"[yellow]![/] skipped {skipped} malformed task.json file(s)")

    if not tasks:
        console.print(f"[dim]No tasks found{f' with status={status}' if status else ''}.[/]")
        return

    console.print(f"[bold]Tasks[/] ({len(tasks)} total{f', status={status}' if status else ''}):\n")
    console.print(f"  {'task_id':<24}  {'status':<14}  {'repo':<16}  {'created'}")
    console.print(f"  {'---':<24}  {'---':<14}  {'---':<16}  {'---'}")
    for task_id, task_status, repo, created in tasks:
        # Color the status.
        if task_status == "passed":
            color = "green"
        elif task_status == "failed":
            color = "red"
        elif task_status == "needs_review":
            color = "yellow"
        elif task_status == "approved":
            color = "cyan"
        elif task_status == "archived":
            color = "dim"
        else:
            color = "white"
        console.print(
            f"  {task_id:<24}  [{color}]{task_status:<14}[/{color}]  {repo:<16}  {created}"
        )


@app.command()
def cleanup(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config (for the repo to clean up).",
    ),
    task_id: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Task id to clean up (e.g. task_20260624_0001).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    force: bool = typer.Option(
        True,
        "--force/--no-force",
        help="Force-remove the worktree even if it has uncommitted changes (default: on, since agent worktrees typically have untracked files).",
    ),
) -> None:
    """Remove the worktree and branch for a completed task.

    Worktrees and branches accumulate across runs. This command cleans up a
    specific task's worktree (via ``git worktree remove``) and its
    ``agent/<task_id>`` branch (via ``git branch -D``). The run directory
    under ``data/runs/<task_id>/`` (events, artifacts, report) is preserved
    — that's the evidence trail, not clutter.
    """
    _require_valid_task_id(task_id)
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc
    repo_path = cfg.repo.path
    store = TaskStore(runs_root=runs_root)
    task_branch = f"agent/{task_id}"
    worktree_path = store.worktree_path(task_id)

    cleaned = []

    # 1. Remove the worktree.
    if worktree_path.exists():
        try:
            from acp.gitops.worktrees import remove_worktree
            remove_worktree(repo_path, worktree_path, force=force)
            cleaned.append(f"worktree: {worktree_path}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗[/] worktree removal failed: {exc}")
    else:
        console.print(f"[dim]worktree not present: {worktree_path}[/]")

    # 2. Delete the branch.
    try:
        from acp.gitops.branches import delete_branch
        delete_branch(repo_path, task_branch, force=force)
        cleaned.append(f"branch: {task_branch}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/] branch removal failed: {exc}")

    for item in cleaned:
        console.print(f"[green]✓[/] removed {item}")
    if cleaned:
        console.print(
            f"\n[dim]Task {task_id} cleaned up. "
            f"Run data preserved at {store.run_dir(task_id)}.[/]"
        )
    else:
        console.print(f"\n[dim]Nothing to clean for task {task_id}.[/]")


if __name__ == "__main__":  # pragma: no cover
    app()
