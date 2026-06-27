"""ACP command-line interface.

``acp run`` is the entry point: it runs one coding task in an isolated git
worktree, captures everything, reviews the diff, and writes an evidence
report + Obsidian note. The orchestration lives in the LangGraph workflow
(``acp.graph.workflow``) — the graph is the only production engine. The
original linear ``EvidenceLoop`` is quarantined in ``acp.legacy_loop`` for
test-only equivalence checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from acp.config import load_repo_config
from acp.errors import ACPError
from acp.events import EventWriter
from acp.evidence.lifecycle import (
    record_lifecycle_event as _record_lifecycle_event,
)
from acp.evidence.lifecycle import (
    rerender_vault_note_from_state as _rerender_vault_note_from_state,
)
from acp.models import EventType, Task, TaskStatus
from acp.store import TaskStore, is_valid_task_id

logger = logging.getLogger(__name__)

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
    mission_id: str = typer.Option(
        "",
        "--mission",
        help="Mission id to link this task to (e.g. mission_20260626_0001).",
    ),
    mission_step: int = typer.Option(
        -1,
        "--mission-step",
        help="Step index (0-based) in the mission this task fulfills.",
    ),
) -> None:
    """Run one coding task in an isolated worktree and write an evidence report.

    When --mission and --mission-step are provided, the task is linked to
    a mission step. The evidence.finalized event will include the hash of
    the preceding step's diff.patch, proving sequential generation (M14
    cross-task artifact sharing).
    """
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold]ACP run[/] · repo={cfg.repo.name} · agent={cfg.agent.default} · task={task!r}"
    )

    # v0.7.0 (M14): Resolve mission context for cross-task artifact sharing.
    parent_task_id = ""
    if mission_id:
        from acp.missions.store import MissionStore, is_valid_mission_id

        if not is_valid_mission_id(mission_id):
            console.print(f"[red]✗[/] invalid mission id: {mission_id!r}")
            raise typer.Exit(code=1)
        store = MissionStore(missions_dir=cfg.mission.missions_dir)
        try:
            parent_task_id = store.get_parent_task_id(mission_id, mission_step)
        except FileNotFoundError:
            console.print(f"[red]✗[/] mission not found: {mission_id}")
            raise typer.Exit(code=1) from None
        console.print(
            f"  mission: {mission_id} (step {mission_step})"
            + (f" → parent: {parent_task_id}" if parent_task_id else "")
        )

    try:
        from acp.graph.workflow import run_workflow

        result = run_workflow(
            config=cfg,
            user_request=task,
            runs_root=runs_root,
            vault_root=vault,
            mission_id=mission_id,
            mission_step_index=mission_step,
            parent_task_id=parent_task_id,
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
            error = result.get("error")
            if error:
                console.print(f"[yellow]![/] reason: {error}")
            console.print(
                f"\n[dim]Task {result.get('task_id')} → needs review. "
                f"Review the vault note and set approved: true to promote memory.[/]"
            )
        else:
            console.print(f"[red]✗[/] report: {report_path or '(missing)'}")
            console.print(f"[red]✗[/] vault: {vault_note_path or '(missing)'}")
            error = result.get("error", "unknown")
            console.print(f"\n[dim]Task {result.get('task_id')} → failed: {error}.[/]")
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
            console.print("[red]✗[/] task.json is malformed — cannot read task_id")
            all_ok = False
    else:
        console.print("[yellow]![/] task.json not found — cannot verify task_id binding")
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
                f"[red]✗[/] event log malformed at line(s): "
                f"{', '.join(str(n) for n in malformed_lines[:5])}"
            )
            all_ok = False
            has_malformed_events = True
        elif not events:
            console.print("[red]✗[/] event log is empty")
            all_ok = False
        elif verify_event_chain(events):
            console.print(
                f"[green]✓[/] event chain valid ({len(events)} events, "
                f"head={events[-1].hash[:16]}...)"
            )
        else:
            console.print("[red]✗[/] event chain INVALID — log has been tampered with")
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
    # v0.6.0: auto.approved and auto.merged are also lifecycle events.
    # v0.6.8: auto.merge.refused is a lifecycle event (it downgrades the
    # task to NEEDS_REVIEW, so it affects the derived status).
    lifecycle_types = {
        "human.approved",
        "human.rejected",
        "memory.promoted",
        "auto.approved",
        "auto.merged",
        "auto.merge.refused",
    }
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
        except Exception as exc:
            logger.warning("status consistency check failed: %s", exc)

    # 2. Evidence manifest — required for v0.5.10+ runs (those with
    # evidence.finalized). Missing manifest is fatal, not a warning.
    manifest_path = run_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        if has_evidence_finalized:
            # v0.5.10+ run — the manifest is required evidence. Its absence
            # means the evidence set has been tampered with.
            console.print(
                "[red]✗[/] evidence manifest not found — required for runs with evidence.finalized"
            )
            all_ok = False
        else:
            console.print(
                "[yellow]![/] evidence manifest not found (runs before v0.5.5 don't have one)"
            )
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
            console.print("[red]✗[/] evidence manifest is malformed JSON")
            all_ok = False

        if verify_evidence_manifest(run_dir, deep=deep):
            mode_label = "deep" if deep else "fast"
            console.print(
                f"[green]✓[/] evidence manifest valid ({mode_label} mode: "
                f"artifacts + report + task.json + event chain + manifest_hash match)"
            )
        else:
            console.print(
                "[red]✗[/] evidence manifest INVALID — artifacts, report, task.json, "
                "event log, or manifest_hash don't match"
            )
            all_ok = False

    # 3. Ed25519 signatures (optional). If the event log is malformed, we
    # skip the "signatures valid" success message — printing it would be
    # misleading, since it only applies to the parsed subset, not the full log.
    if public_key is not None:
        if not events_path.is_file():
            console.print("[red]✗[/] cannot verify signatures without event log")
            all_ok = False
        elif has_malformed_events:
            console.print("[red]✗[/] signature verification skipped because event log is malformed")
            all_ok = False
        else:
            try:
                pk_bytes = public_key.read_bytes()
                if len(pk_bytes) != 32:
                    console.print(
                        f"[red]✗[/] public key must be exactly 32 bytes, got {len(pk_bytes)}"
                    )
                    all_ok = False
                elif not events:
                    console.print("[red]✗[/] cannot verify signatures on empty event log")
                    all_ok = False
                elif verify_event_signatures(events, pk_bytes):
                    console.print(
                        f"[green]✓[/] Ed25519 signatures valid ({len(events)} events signed)"
                    )
                else:
                    console.print("[red]✗[/] Ed25519 signature verification FAILED")
                    all_ok = False
            except ImportError:
                console.print(
                    "[yellow]![/] cryptography package not installed — "
                    "install with: uv sync --extra crypto"
                )
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
                "[red]✗[/] lifecycle manifest not found — required when lifecycle "
                "events (approve/reject/promote) exist in the event log"
            )
            all_ok = False
        elif verify_lifecycle_manifest(run_dir):
            console.print("[green]✓[/] lifecycle manifest valid")
        else:
            console.print("[red]✗[/] lifecycle manifest INVALID — lifecycle events don't match")
            all_ok = False
    elif lifecycle_path.is_file():
        # Lifecycle manifest exists but no lifecycle events — verify it's valid.
        if verify_lifecycle_manifest(run_dir):
            console.print("[green]✓[/] lifecycle manifest valid")
        else:
            console.print("[red]✗[/] lifecycle manifest INVALID — lifecycle events don't match")
            all_ok = False

    # Report final approval state if present.
    if events:
        from acp.models import EventType as _ET

        approved = any(e.type == _ET.HUMAN_APPROVED or e.type == _ET.AUTO_APPROVED for e in events)
        rejected = any(e.type == _ET.HUMAN_REJECTED for e in events)
        if approved:
            console.print("[dim]Final approval state: approved[/]")
        elif rejected:
            console.print("[dim]Final approval state: rejected[/]")

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
    console.print(
        f"  {'#':>3}  {'event_id':<14}  {'type':<24}  {'timestamp':<22}  {'hash (first 12)'}"
    )
    console.print(f"  {'---':>3}  {'---':<14}  {'---':<24}  {'---':<22}  {'---'}")
    for i, evt in enumerate(all_events):
        short_hash = evt.hash[:12] if evt.hash else "—"
        signed = " ✓" if evt.signature else ""
        console.print(
            f"  {i + 1:>3}  {evt.event_id:<14}  {evt.type.value:<24}  "
            f"{evt.timestamp:<22}  {short_hash}{signed}"
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
        on_warning=lambda msg: console.print(f"[yellow]![/] {msg}"),
    )

    console.print(f"[green]✓[/] task {task_id} approved by {approver or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print("  memory_status: active")
    console.print(f"  event: human.approved written to {store.events_path(task_id)}")
    console.print("  lifecycle evidence written — `acp verify` remains valid")


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
        console.print("[red]✗[/] task is already approved — cannot reject after approval.")
        raise typer.Exit(code=1)
    if task.status == TaskStatus.REJECTED:
        console.print("[red]✗[/] task is already rejected — cannot reject again.")
        raise typer.Exit(code=1)
    if task.status == TaskStatus.ARCHIVED:
        console.print("[red]✗[/] task is already archived — cannot reject again.")
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
        on_warning=lambda msg: console.print(f"[yellow]![/] {msg}"),
    )

    console.print(f"[green]✓[/] task {task_id} rejected by {rejecter or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print("  memory_status: archived")
    console.print(f"  event: human.rejected written to {store.events_path(task_id)}")
    console.print("  lifecycle evidence written — `acp verify` remains valid")


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
        help=(
            "Force-remove the worktree even if it has uncommitted changes "
            "(default: on, since agent worktrees typically have untracked files)."
        ),
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
            f"\n[dim]Task {task_id} cleaned up. Run data preserved at {store.run_dir(task_id)}.[/]"
        )
    else:
        console.print(f"\n[dim]Nothing to clean for task {task_id}.[/]")


@app.command()
def archive(
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    older_than_days: int = typer.Option(
        30,
        "--older-than",
        "-n",
        help="Archive runs older than N days (default: 30).",
    ),
    archive_dir: Path = typer.Option(
        Path("data/archive"),
        "--archive-dir",
        help="Directory to store archived runs (default: data/archive).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be archived without actually moving or compressing.",
    ),
    compress: bool = typer.Option(
        True,
        "--compress/--no-compress",
        help="Compress archived runs as .tar.gz (default: on).",
    ),
    keep: bool = typer.Option(
        False,
        "--keep",
        help="Keep the original run directory after archiving (default: remove).",
    ),
) -> None:
    """Archive old run data to free up disk space.

    v0.7.5: Moves run directories older than ``--older-than`` days from
    ``data/runs/`` to ``data/archive/`` (or compresses them as .tar.gz).
    This prevents ``data/runs/`` from growing indefinitely as tasks accumulate.

    The evidence trail is preserved — archived runs can be restored by
    simply moving them back. When ``--compress`` is on (default), each
    run is stored as ``<archive_dir>/<task_id>.tar.gz``.

    Use ``--dry-run`` to preview which runs would be archived.

    Skips runs that are not in a terminal status (completed, failed,
    rejected, merged). In-progress runs are never archived.
    """
    import shutil
    import tarfile
    from datetime import datetime, timedelta

    if not runs_root.is_dir():
        console.print(f"[red]✗[/] runs root not found: {runs_root}")
        raise typer.Exit(code=1)

    cutoff = datetime.now() - timedelta(days=older_than_days)
    cutoff_ts = cutoff.timestamp()

    # Terminal statuses — only these can be archived.
    terminal_statuses = {"completed", "failed", "rejected", "merged", "needs_review"}

    candidates: list[tuple[str, Path, float]] = []
    for task_dir in sorted(runs_root.iterdir()):
        if not task_dir.is_dir():
            continue
        task_json = task_dir / "task.json"
        if not task_json.is_file():
            continue
        try:
            import json as _json

            task_data = _json.loads(task_json.read_text())
            status = task_data.get("status", "")
            if status not in terminal_statuses:
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive: skipping %s (cannot read status): %s", task_dir.name, exc)
            continue
        try:
            mtime = task_json.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            candidates.append((task_dir.name, task_dir, mtime))

    if not candidates:
        console.print(f"[dim]No runs older than {older_than_days} days to archive.[/]")
        return

    mode_label = "DRY-RUN" if dry_run else ("COMPRESS" if compress else "MOVE")
    console.print(
        f"[bold]ACP archive[/] · mode={mode_label} · older_than={older_than_days}d "
        f"· archive_dir={archive_dir}"
    )
    console.print(f"[dim]Found {len(candidates)} runs to archive:[/]\n")

    archived = 0
    failed = 0
    total_freed = 0

    for tid, task_dir, mtime in candidates:
        age_days = int((datetime.now().timestamp() - mtime) / 86400)
        size = sum(f.stat().st_size for f in task_dir.rglob("*") if f.is_file())
        size_label = f"{size / 1024:.0f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"

        if dry_run:
            console.print(f"  [dim]{tid:<24}  {age_days}d old  {size_label:>8}  (dry-run)[/]")
            total_freed += size
            continue

        try:
            archive_dir.mkdir(parents=True, exist_ok=True)

            if compress:
                archive_path = archive_dir / f"{tid}.tar.gz"
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(task_dir, arcname=tid)
                if not keep:
                    shutil.rmtree(task_dir)
                console.print(
                    f"  [green]✓[/] {tid:<24}  {age_days}d old  {size_label:>8}"
                    f"  → {archive_path.name}"
                )
            else:
                target = archive_dir / tid
                shutil.move(str(task_dir), str(target))
                console.print(
                    f"  [green]✓[/] {tid:<24}  {age_days}d old  {size_label:>8}  → {target}"
                )

            total_freed += size
            archived += 1
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]✗[/] {tid:<24}  failed: {exc}")
            failed += 1

    if dry_run:
        console.print(
            f"\n[dim]Dry-run complete. {len(candidates)} runs ({total_freed / 1024:.0f}KB) "
            f"would be archived.[/]"
        )
    else:
        freed_label = (
            f"{total_freed / 1024:.0f}KB"
            if total_freed < 1024 * 1024
            else f"{total_freed / 1024 / 1024:.1f}MB"
        )
        console.print(
            f"\n[green]Archived {archived} runs[/] ({freed_label} freed). {failed} failed."
        )


# --------------------------------------------------------------------------- #
# v0.6.2 (M7): acp memory — Graphiti temporal memory promotion.
# --------------------------------------------------------------------------- #

memory_app = typer.Typer(
    name="memory",
    help="Manage temporal memory (Graphiti + FalkorDB).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(memory_app, name="memory")


@memory_app.command("promote")
def memory_promote(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    vault_root: Path = typer.Option(
        Path("vault"),
        "--vault-root",
        help="Root directory of the Obsidian vault (default: vault).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    task_id: str = typer.Option(
        "",
        "--task",
        "-t",
        help="Promote a specific task (default: scan all eligible notes).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be promoted without actually ingesting.",
    ),
) -> None:
    """Promote approved vault notes to Graphiti temporal memory.

    Scans ``vault/tasks/`` for notes that are:
      - ``approved: true``
      - ``memory_status: active``
      - ``graphiti_ingested: false``

    And ingests them into FalkorDB via Graphiti. After ingestion, the
    note's frontmatter is updated to ``graphiti_ingested: true`` and a
    ``memory.promoted`` event is written to the event log.

    Requires:
      - ``uv sync --extra memory`` (graphiti-core[falkordb])
      - FalkorDB running: ``docker run -p 6379:6379 falkordb/falkordb``
      - ``OPENAI_API_KEY`` env var (for Graphiti's LLM entity extraction)
    """
    from acp.memory.promotion_rules import (
        get_promotion_metadata,
        should_promote_to_graphiti,
    )
    from acp.vault.frontmatter import parse_frontmatter

    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc

    group_id = cfg.memory.graphiti_group_id
    tasks_dir = vault_root / "tasks"

    if not tasks_dir.is_dir():
        console.print(f"[red]✗[/] vault tasks directory not found: {tasks_dir}")
        raise typer.Exit(code=1)

    # Collect candidate notes.
    if task_id:
        _require_valid_task_id(task_id)
        candidates = [tasks_dir / f"{task_id}.md"]
    else:
        candidates = sorted(tasks_dir.glob("*.md"))

    if not candidates:
        console.print(f"[dim]No vault notes found in {tasks_dir}[/]")
        return

    # Filter to eligible notes.
    store = TaskStore(runs_root=runs_root)
    eligible: list[tuple[Task, Path, dict[str, Any]]] = []

    for note_path in candidates:
        if not note_path.is_file():
            continue
        try:
            content = note_path.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(content)
        except (ValueError, OSError):
            continue

        tid = fm.task_id or note_path.stem
        try:
            task = store.load(tid)
        except Exception:
            console.print(f"[dim]skip {tid}: task.json not found[/]")
            continue

        if should_promote_to_graphiti(task, fm, note_path):
            meta = get_promotion_metadata(task, fm, note_path)
            eligible.append((task, note_path, meta))

    if not eligible:
        console.print("[dim]No eligible notes to promote.[/]")
        return

    # Show what we'll promote.
    console.print(f"[bold]Found {len(eligible)} eligible note(s):[/]")
    for task, note_path, meta in eligible:
        flags = []
        if meta["is_known_failure"]:
            flags.append("[yellow]known-failure[/]")
        if meta["needs_secondary_review"]:
            flags.append("[red]high-risk[/]")
        flag_str = f" {' '.join(flags)}" if flags else ""
        console.print(f"  {task.task_id} — {task.user_request[:60]}{flag_str}")

    if dry_run:
        console.print("\n[dim]--dry-run: no ingestion performed.[/]")
        return

    # Ingest each eligible note.
    try:
        from acp.memory.graphiti_client import ingest_task_to_graphiti
    except ImportError as exc:
        console.print(
            f"[red]✗[/] graphiti-core not installed: {exc}\n  Install with: uv sync --extra memory"
        )
        raise typer.Exit(code=1) from exc

    promoted = 0
    failed = 0

    for task, note_path, meta in eligible:
        tid = task.task_id
        try:
            content = note_path.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(content)

            result = asyncio.run(
                ingest_task_to_graphiti(
                    task=task,
                    frontmatter=fm,
                    vault_note_path=note_path,
                    graphiti_group_id=group_id,
                    memory_config=cfg.memory,
                )
            )

            # Write memory.promoted event to the event log.
            run_dir = store.run_dir(tid)
            if run_dir.is_dir():
                events = EventWriter(tid, run_dir)
                events.write(
                    EventType.MEMORY_PROMOTED,
                    {
                        "task_id": tid,
                        "episode_id": result.get("episode_id", ""),
                        "nodes_created": result.get("nodes_created", 0),
                        "edges_created": result.get("edges_created", 0),
                        "graphiti_group_id": group_id,
                    },
                )

            console.print(
                f"[green]✓[/] {tid}: ingested "
                f"({result.get('nodes_created', 0)} nodes, "
                f"{result.get('edges_created', 0)} edges)"
            )
            promoted += 1
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗[/] {tid}: ingestion failed — {exc}")
            failed += 1

    console.print(
        f"\n[bold]Promoted {promoted} note(s)" + (f", {failed} failed" if failed else "") + ".[/]"
    )


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(
        ...,
        help="Natural language query (e.g., 'authentication login changes').",
    ),
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    num_results: int = typer.Option(
        10,
        "--num-results",
        "-n",
        help="Maximum number of results (default: 10).",
    ),
) -> None:
    """Search Graphiti temporal memory for facts.

    Queries FalkorDB for active, non-superseded facts related to the
    query. Results show what the system "remembers" about the codebase.

    Requires:
      - ``uv sync --extra memory`` (graphiti-core[falkordb])
      - FalkorDB running: ``docker run -p 6379:6379 falkordb/falkordb``
    """
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc

    group_id = cfg.memory.graphiti_group_id

    try:
        from acp.memory.graphiti_client import search_graphiti_facts
    except ImportError as exc:
        console.print(
            f"[red]✗[/] graphiti-core not installed: {exc}\n  Install with: uv sync --extra memory"
        )
        raise typer.Exit(code=1) from exc

    try:
        results = asyncio.run(
            search_graphiti_facts(
                query,
                group_id=group_id,
                num_results=num_results,
                memory_config=cfg.memory,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/] search failed: {exc}")
        raise typer.Exit(code=1) from exc

    if not results:
        console.print("[dim]No facts found.[/]")
        return

    console.print(f"[bold]Found {len(results)} fact(s):[/]")
    for i, fact in enumerate(results, 1):
        console.print(f"\n  [bold]#{i}[/]")
        console.print(f"  fact: {fact.get('fact', '?')}")
        console.print(f"  source: {fact.get('source_node', '?')}")
        console.print(f"  target: {fact.get('target_node', '?')}")
        if fact.get("valid_at"):
            console.print(f"  valid_at: {fact['valid_at']}")


@memory_app.command("prune")
def memory_prune(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    older_than_days: int = typer.Option(
        90,
        "--older-than-days",
        help="Only prune nodes superseded more than this many days ago (default: 90).",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="When True (default), only report what would be pruned. "
        "Use --no-dry-run to actually delete nodes.",
    ),
) -> None:
    """Prune superseded nodes from the Graphiti/FalkorDB knowledge graph.

    Over time, Graphiti accumulates superseded facts — old versions of
    truths that have been replaced by newer ones. This command identifies
    nodes that have been superseded for more than --older-than-days and
    optionally deletes them, preventing unbounded knowledge graph growth.

    By default, runs in --dry-run mode (reports only, no deletion).
    Use --no-dry-run to actually delete nodes.

    Requires:
      - ``uv sync --extra memory`` (graphiti-core[falkordb])
      - FalkorDB running: ``docker run -p 6379:6379 falkordb/falkordb``
    """
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc

    group_id = cfg.memory.graphiti_group_id

    try:
        from acp.memory.graphiti_client import prune_superseded_nodes
    except ImportError as exc:
        console.print(
            f"[red]✗[/] graphiti-core not installed: {exc}\n  Install with: uv sync --extra memory"
        )
        raise typer.Exit(code=1) from exc

    mode_label = "dry-run" if dry_run else "DELETE"
    console.print(
        f"[bold]ACP memory prune[/] · mode={mode_label} · older_than_days={older_than_days}"
    )

    try:
        result = asyncio.run(
            prune_superseded_nodes(
                group_id=group_id,
                older_than_days=older_than_days,
                dry_run=dry_run,
                memory_config=cfg.memory,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/] prune failed: {exc}")
        raise typer.Exit(code=1) from exc

    found = result["found"]
    pruned = result["pruned"]
    nodes = result["nodes"]

    if found == 0:
        console.print("[green]✓[/] no superseded nodes found (graph is clean)")
        return

    console.print(
        f"[yellow]![/] found {found} superseded node(s) older than {older_than_days} days"
    )
    for node in nodes[:20]:  # show first 20
        console.print(f"  node: {node['node_id']} · superseded {node['days_superseded']} days ago")
    if len(nodes) > 20:
        console.print(f"  ... and {len(nodes) - 20} more")

    if dry_run:
        console.print("\n[dim]Dry run — no nodes deleted. Use --no-dry-run to actually prune.[/]")
    else:
        console.print(f"\n[green]✓[/] pruned {pruned} node(s) from FalkorDB")


# --------------------------------------------------------------------------- #
# v0.7.0 (Phase 1.1): acp migrate — SQLite-as-primary task store migration.
# --------------------------------------------------------------------------- #


@app.command("migrate")
def migrate_to_sqlite(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory of run data (default: data/runs).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="When set, only report what would be migrated without writing to SQLite.",
    ),
) -> None:
    """Migrate task.json files into the SQLite durable task store.

    Imports all existing task.json files under --runs-root into the
    SQLite database configured at evidence.durable_store. Emits a
    task.store_migrated event for each imported task, creating a
    cryptographically-bound audit trail of the migration.

    After migration, set evidence.task_store_primary: "sqlite" in the
    repo config to make SQLite the primary source of truth for task
    state. task.json files continue to be written as a projection.

    Use --dry-run to preview without writing.
    """
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc

    if cfg.evidence.durable_store is None:
        console.print(
            "[red]✗[/] evidence.durable_store is not configured. "
            "Set it in the repo config before migrating."
        )
        raise typer.Exit(code=1)

    from acp.events import EventWriter
    from acp.evidence.durable_task_store import DurableTaskStore
    from acp.models import EventType

    store = DurableTaskStore(cfg.evidence.durable_store)
    store.init()

    if dry_run:
        # Count task.json files without importing.
        count = sum(1 for _ in Path(runs_root).rglob("task.json"))
        console.print(f"[bold]ACP migrate[/] · dry-run · found {count} task.json file(s)")
        console.print(f"[dim]Use without --dry-run to import into {cfg.evidence.durable_store}[/]")
        store.close()
        return

    imported = store.rebuild_from_jsonl(runs_root)
    console.print(f"[green]✓[/] migrated {imported} task(s) into {cfg.evidence.durable_store}")

    # Emit task.store_migrated event. EventWriter writes to
    # run_dir / "events.jsonl", so we use a dedicated migration directory.
    migration_dir = Path(runs_root) / ".migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    events = EventWriter("__migration__", migration_dir)
    events.write(
        EventType.TASK_STORE_MIGRATED,
        {
            "imported_count": imported,
            "durable_store": str(cfg.evidence.durable_store),
            "runs_root": str(runs_root),
            "primary": cfg.evidence.task_store_primary,
        },
    )

    console.print(
        f"[green]✓[/] task.store_migrated event written to {migration_dir / 'events.jsonl'}"
    )
    console.print(
        '\n[dim]Set evidence.task_store_primary: "sqlite" in the '
        "repo config to make SQLite the primary source of truth.[/]"
    )
    store.close()


# --------------------------------------------------------------------------- #
# v0.7.0 (M14): acp mission — group tasks into larger epics.
# --------------------------------------------------------------------------- #

mission_app = typer.Typer(
    name="mission",
    help="Manage missions — overarching goals split into sequential task runs.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(mission_app, name="mission")


def _require_valid_mission_id(mission_id: str) -> None:
    """Exit nonzero if ``mission_id`` is not a canonical mission id."""
    from acp.missions.store import is_valid_mission_id

    if not is_valid_mission_id(mission_id):
        console.print(
            f"[red]✗[/] invalid mission id: {mission_id!r} "
            f"(expected mission_<YYYYMMDD>_<NNNN>, e.g. mission_20260626_0001)"
        )
        raise typer.Exit(code=1)


@mission_app.command("create")
def mission_create(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config (for repo name + path).",
    ),
    goal: str = typer.Option(
        ...,
        "--goal",
        "-g",
        help="The overarching mission goal (e.g. 'Migrate to React 19').",
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Optional longer description of the mission.",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data (default: data/missions).",
    ),
) -> None:
    """Create a new mission from an overarching goal.

    Writes ``mission.yaml`` and a ``mission.created`` event to
    ``<missions_dir>/<mission_id>/``. Use ``acp mission split`` to add
    steps, then spawn tasks for each step.
    """
    try:
        cfg = load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc

    from acp.missions.store import MissionStore

    store = MissionStore(missions_dir=missions_dir)
    mission_id = store.next_mission_id()

    try:
        mission = store.create(
            mission_id=mission_id,
            goal=goal,
            repo_name=cfg.repo.name,
            repo_path=cfg.repo.path,
            base_branch=cfg.repo.default_branch,
            description=description,
        )
    except Exception as exc:
        console.print(f"[red]✗[/] mission creation failed: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✓[/] mission {mission_id} created")
    console.print(f"  goal: {mission.goal}")
    console.print(f"  repo: {mission.repo_name} (branch: {mission.base_branch})")
    console.print(f"  dir:  {store.mission_dir(mission_id)}")
    console.print(f"  event: mission.created written to {store.events_path(mission_id)}")
    console.print(
        f'\n[dim]Next: add steps with `acp mission split --mission {mission_id} --step "..."`[/]'
    )


@mission_app.command("list")
def mission_list(
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data (default: data/missions).",
    ),
) -> None:
    """List all missions."""
    from acp.missions.store import MissionStore

    store = MissionStore(missions_dir=missions_dir)
    if not store.root.is_dir():
        console.print(f"[dim]No missions directory found at {store.root}[/]")
        return

    missions = store.list_missions()
    if not missions:
        console.print("[dim]No missions found.[/]")
        return

    console.print(f"[bold]Missions[/] ({len(missions)} total):\n")
    console.print(f"  {'mission_id':<28}  {'status':<14}  {'steps':<6}  {'goal'}")
    console.print(f"  {'---':<28}  {'---':<14}  {'---':<6}  {'---'}")
    for m in missions:
        completed = sum(1 for s in m.steps if s.status == "completed")
        step_str = f"{completed}/{len(m.steps)}"
        color = (
            "green"
            if m.status.value == "completed"
            else ("cyan" if m.status.value == "in_progress" else "white")
        )
        console.print(
            f"  {m.mission_id:<28}  [{color}]{m.status.value:<14}[/{color}]  "
            f"{step_str:<6}  {m.goal[:50]}"
        )


@mission_app.command("show")
def mission_show(
    mission_id: str = typer.Option(
        ...,
        "--mission",
        "-m",
        help="Mission id to show (e.g. mission_20260626_0001).",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data (default: data/missions).",
    ),
) -> None:
    """Show details of a specific mission, including its steps."""
    _require_valid_mission_id(mission_id)

    from acp.missions.store import MissionStore

    store = MissionStore(missions_dir=missions_dir)
    try:
        mission = store.load(mission_id)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] mission not found: {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]✗[/] cannot load mission: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]Mission {mission.mission_id}[/]")
    console.print(f"  status:      {mission.status.value}")
    console.print(f"  goal:        {mission.goal}")
    if mission.description:
        console.print(f"  description: {mission.description}")
    console.print(f"  repo:        {mission.repo_name} (branch: {mission.base_branch})")
    console.print(f"  created:     {mission.created_at}")
    if mission.completed_at:
        console.print(f"  completed:   {mission.completed_at}")

    if not mission.steps:
        console.print("\n  [dim]No steps yet. Add with `acp mission split`.[/]")
    else:
        console.print(f"\n  [bold]Steps[/] ({len(mission.steps)}):")
        for i, step in enumerate(mission.steps):
            status_color = {
                "pending": "dim",
                "running": "yellow",
                "completed": "green",
                "failed": "red",
            }.get(step.status, "white")
            task_str = f" → {step.task_id}" if step.task_id else ""
            console.print(
                f"    {i + 1}. [{status_color}]{step.status}[/{status_color}] "
                f"{step.description}{task_str}"
            )


@mission_app.command("split")
def mission_split(
    mission_id: str = typer.Option(
        ...,
        "--mission",
        "-m",
        help="Mission id to add a step to (e.g. mission_20260626_0001).",
    ),
    step: str = typer.Option(
        ...,
        "--step",
        "-s",
        help="Description of the step to add (becomes a task when spawned).",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data (default: data/missions).",
    ),
) -> None:
    """Add a step to a mission.

    Each step is a sequential sub-task of the mission. Steps are executed
    in order — step N+1 can read the artifacts of step N (cross-task
    artifact sharing). Use ``acp run`` to spawn the actual task for a
    step once the mission plan is ready.
    """
    _require_valid_mission_id(mission_id)

    from acp.missions.store import MissionStore

    store = MissionStore(missions_dir=missions_dir)
    try:
        mission = store.add_step(mission_id, step)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] mission not found: {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]✗[/] failed to add step: {exc}")
        raise typer.Exit(code=1) from exc

    step_num = len(mission.steps)
    console.print(f"[green]✓[/] step {step_num} added to mission {mission_id}")
    console.print(f"  description: {step}")
    console.print("  status: pending")
    console.print(
        f"\n[dim]{step_num} step(s) total. View with `acp mission show --mission {mission_id}`[/]"
    )


@mission_app.command("complete")
def mission_complete(
    mission_id: str = typer.Option(
        ...,
        "--mission",
        "-m",
        help="Mission id to complete (e.g. mission_20260626_0001).",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data (default: data/missions).",
    ),
) -> None:
    """Mark a mission as completed.

    All steps must be in a terminal state (completed or failed) before
    a mission can be completed. Writes a ``mission.completed`` event to
    the mission's event log.
    """
    _require_valid_mission_id(mission_id)

    from acp.missions.store import MissionStore

    store = MissionStore(missions_dir=missions_dir)
    try:
        mission = store.complete(mission_id)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] mission not found: {exc}")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        console.print(f"[red]✗[/] cannot complete: {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]✗[/] completion failed: {exc}")
        raise typer.Exit(code=1) from exc

    completed = sum(1 for s in mission.steps if s.status == "completed")
    failed = sum(1 for s in mission.steps if s.status == "failed")
    console.print(f"[green]✓[/] mission {mission_id} completed")
    console.print(f"  steps: {completed} completed, {failed} failed, {len(mission.steps)} total")
    console.print(f"  event: mission.completed written to {store.events_path(mission_id)}")


@mission_app.command("run")
def mission_run(
    mission_id: str = typer.Option(
        ...,
        "--mission",
        "-m",
        help="Mission id to run (e.g. mission_20260626_0001).",
    ),
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data (default: data/missions).",
    ),
    runs_root: Path = typer.Option(
        Path("data/runs"),
        "--runs-root",
        help="Root directory for task runs (default: data/runs).",
    ),
    vault_root: Path = typer.Option(
        Path("data/vault"),
        "--vault-root",
        help="Root directory for vault notes (default: data/vault).",
    ),
) -> None:
    """Run all pending steps in a mission sequentially (v0.8.0, Phase 3.1).

    Each step is executed via the ACP workflow with cross-task artifact
    chaining — each step's evidence includes the sha256 of the preceding
    step's diff.patch. Mission-level events (step_started, step_completed,
    step_failed) are written to the mission's event log for real-time
    progress tracking.

    The mission can be paused between steps by setting its status to
    ``paused`` (via the API or by editing mission.yaml). The orchestrator
    stops after the current step completes and can be resumed with
    ``acp mission run`` again.
    """
    _require_valid_mission_id(mission_id)

    from acp.missions.orchestrator import MissionOrchestrator

    orchestrator = MissionOrchestrator(
        config_path=config,
        missions_dir=missions_dir,
        runs_root=runs_root,
        vault_root=vault_root,
    )
    try:
        result = orchestrator.run(mission_id)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] mission not found: {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/] mission run failed: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✓[/] mission {mission_id} run complete")
    console.print(f"  steps run: {result['steps_run']}")
    console.print(f"  passed: {result['steps_passed']}")
    console.print(f"  failed: {result['steps_failed']}")
    if result.get("paused"):
        console.print("[yellow]  mission paused — resume with `acp mission run`[/]")
    if result.get("message"):
        console.print(f"  {result['message']}")


@mission_app.command("pause")
def mission_pause(
    mission_id: str = typer.Option(
        ...,
        "--mission",
        "-m",
        help="Mission id to pause.",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data.",
    ),
) -> None:
    """Pause a running mission — stops after the current step completes."""
    _require_valid_mission_id(mission_id)

    from acp.missions.orchestrator import MissionOrchestrator

    orchestrator = MissionOrchestrator(
        config_path=Path(".repo.yaml"),  # not needed for pause
        missions_dir=missions_dir,
    )
    try:
        orchestrator.pause(mission_id)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] mission not found: {exc}")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        console.print(f"[red]✗[/] cannot pause: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[yellow]⏸[/] mission {mission_id} paused (will stop after current step)")


@mission_app.command("resume")
def mission_resume(
    mission_id: str = typer.Option(
        ...,
        "--mission",
        "-m",
        help="Mission id to resume.",
    ),
    missions_dir: Path = typer.Option(
        Path("data/missions"),
        "--missions-dir",
        help="Root directory for mission data.",
    ),
) -> None:
    """Resume a paused mission."""
    _require_valid_mission_id(mission_id)

    from acp.missions.orchestrator import MissionOrchestrator

    orchestrator = MissionOrchestrator(
        config_path=Path(".repo.yaml"),  # not needed for resume
        missions_dir=missions_dir,
    )
    try:
        orchestrator.resume(mission_id)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] mission not found: {exc}")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        console.print(f"[red]✗[/] cannot resume: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]▶[/] mission {mission_id} resumed (status=running)")


# --------------------------------------------------------------------------- #
# v0.6.5 (M10): acp serve — FastAPI HTTP control layer.
# --------------------------------------------------------------------------- #


@app.command()
def serve(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a <name>.repo.yaml repo config.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address (default: 127.0.0.1 — localhost only).",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        "-p",
        help="Port to listen on (default: 8000).",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Enable auto-reload for development.",
    ),
    api_token: str = typer.Option(
        "",
        "--api-token",
        help=(
            "Bearer token for API authentication. When set, all non-public "
            "endpoints require an 'Authorization: Bearer <token>' header. "
            "Can also be set via the ACP_API_TOKEN env var. If neither is "
            "set, auth is disabled (local dev only)."
        ),
    ),
) -> None:
    """Start the ACP HTTP API server.

    Exposes the ACP workflow as a local HTTP API. Requires the ``api``
    optional dependency group::

        uv sync --extra api

    Endpoints:
      POST /tasks/run          — run a coding task
      GET  /tasks              — list all tasks
      GET  /tasks/{id}         — get task status
      POST /tasks/{id}/approve — approve a vault note
      POST /tasks/{id}/reject  — reject a vault note
      GET  /tasks/{id}/events  — get event log
      GET  /tasks/{id}/report  — get report content
      GET  /memory/search      — search temporal memory
      GET  /health             — health check (no auth required)

    The server binds to 127.0.0.1 (localhost) by default. Do NOT expose
    to the network without authentication — this API can run arbitrary
    code via POST /tasks/run. Use ``--api-token`` or set the
    ``ACP_API_TOKEN`` env var to enable bearer token auth.
    """
    try:
        from acp.api.server import set_api_token
        from acp.api.server import state as server_state
    except ImportError as exc:
        console.print(
            f"[red]✗[/] FastAPI not installed: {exc}\n  Install with: uv sync --extra api"
        )
        raise typer.Exit(code=1) from exc

    # Validate the config loads before starting the server.
    try:
        load_repo_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/] config file not found: {exc}")
        raise typer.Exit(code=1) from exc

    server_state.set_config(str(config))

    # Configure bearer token auth if provided via --api-token or env var.
    if api_token:
        set_api_token(api_token)

    # v0.7.4: Hard-block binding to a non-localhost interface without an
    # API token. POST /tasks/run accepts arbitrary prompts and configs —
    # exposing it on 0.0.0.0 without auth is instant RCE for anyone on
    # the network. Fail closed instead of printing a warning.
    from acp.api.server import get_api_token

    _is_localhost = host in ("127.0.0.1", "localhost", "::1")
    if not _is_localhost and get_api_token() is None:
        console.print(
            f"[red]✗[/] REFUSING to start: --host {host} binds to a non-localhost "
            f"interface, but no API token is set.\n"
            f"  POST /tasks/run accepts arbitrary commands — exposing it without "
            f"auth is remote code execution.\n"
            f"  Either:\n"
            f"    1. Use --host 127.0.0.1 (default, localhost only)\n"
            f"    2. Set --api-token <secret> to require bearer auth\n"
            f"    3. Set ACP_API_TOKEN=<secret> environment variable"
        )
        raise typer.Exit(code=1)

    # Read CORS settings from the repo config and set env vars so the
    # server module picks them up at import time (uvicorn imports the app
    # module fresh, so env vars set here are visible to the server).
    cfg = load_repo_config(config)
    if not cfg.api.cors_enabled:
        os.environ["ACP_CORS_ENABLED"] = "false"
    elif cfg.api.cors_origins:
        os.environ["ACP_CORS_ORIGINS"] = ",".join(cfg.api.cors_origins)

    console.print(f"[bold]ACP API server[/] · config={config}")
    console.print(f"  listening on http://{host}:{port}")
    console.print(f"  docs at http://{host}:{port}/docs")

    if get_api_token() is not None:
        console.print("  [green]auth: bearer token enabled[/]")
    else:
        console.print("  [dim]auth: disabled (localhost only — safe)[/]")
    if not cfg.api.cors_enabled:
        console.print("  [dim]CORS: disabled (same-origin)[/]")
    elif cfg.api.cors_origins:
        console.print(f"  [dim]CORS: {', '.join(cfg.api.cors_origins)}[/]")
    else:
        console.print("  [dim]CORS: dev defaults (localhost:5173, :3000)[/]")
    console.print("  [dim]Ctrl+C to stop[/]")

    import uvicorn

    uvicorn.run(
        "acp.api.server:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
