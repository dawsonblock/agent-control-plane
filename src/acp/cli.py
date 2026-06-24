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

    Used when a lifecycle event write fails after the vault note has already
    been modified. The event log is the source of truth — a modified vault note
    without a corresponding event is an inconsistent state that must not persist.
    Revert failures are logged but not raised (we're already in an error path).
    """
    try:
        note_path.write_text(original_content)
    except Exception:  # noqa: BLE001
        console.print(f"[red]✗[/] could not revert vault note: {note_path}")


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
) -> None:
    """Append a post-run lifecycle event and keep the evidence manifest valid."""
    from acp.errors import EvidenceConfigError
    from acp.evidence.manifest import read_evidence_config, write_evidence_manifest

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
            # Pre-v0.5.9 signed run with no sidecar. We know it was signed but
            # don't know which key was used. Fail closed — refuse to write an
            # unsigned lifecycle event that would break signature verification.
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

    evt = events.write(event_type, payload)

    # Dual-write to the run's durable store if it had one. Best-effort: the
    # SQLite store is a derived index, rebuildable from the JSONL log.
    durable_store_path = ev_cfg["durable_store"]
    if durable_store_path is not None:
        try:
            from acp.evidence.durable_store import DurableEventStore
            with DurableEventStore(durable_store_path) as db:
                db.append(evt)
        except Exception:  # noqa: BLE001
            pass

    # Recompute + rewrite the evidence manifest so its event chain head
    # matches the new last event. ``acp verify`` checks events[-1].hash
    # against manifest.event_chain_head, so this must be current.
    write_evidence_manifest(run_dir=run_dir, events_writer=events)


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
) -> None:
    """Run one coding task in an isolated worktree and write an evidence report."""
    cfg = load_repo_config(config)
    console.print(
        f"[bold]ACP run[/] · repo={cfg.repo.name} · "
        f"agent={cfg.agent.default} · task={task!r}"
    )
    try:
        from acp.graph.workflow import run_workflow
        result = run_workflow(
            config=cfg,
            user_request=task,
            runs_root="data/runs",
            vault_root=vault,
        )
        status = result.get("status")
        report_path = result.get("report_path")
        vault_note_path = result.get("vault_note_path")

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
) -> None:
    """Verify the evidence integrity of a completed run.

    Checks:
      1. Event hash chain is valid (tamper-evident)
      2. Evidence manifest exists and all artifact hashes match
      3. (Optional) Ed25519 event signatures are valid

    Exits 0 if all checks pass, 1 if any fail.
    """
    from acp.events import verify_event_chain, verify_event_signatures
    from acp.evidence.manifest import verify_evidence_manifest
    from acp.models import Event

    _require_valid_task_id(task_id)
    store = TaskStore(runs_root=runs_root)
    run_dir = store.run_dir(task_id)

    if not run_dir.is_dir():
        console.print(f"[red]✗[/] run directory not found: {run_dir}")
        raise typer.Exit(code=1)

    all_ok = True
    events: list[Event] = []

    # 1. Event hash chain.
    events_path = store.events_path(task_id)
    if not events_path.is_file():
        console.print(f"[red]✗[/] event log not found: {events_path}")
        all_ok = False
    else:
        events = [
            Event.model_validate_json(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        if not events:
            console.print(f"[red]✗[/] event log is empty")
            all_ok = False
        elif verify_event_chain(events):
            console.print(f"[green]✓[/] event chain valid ({len(events)} events, head={events[-1].hash[:16]}...)")
        else:
            console.print(f"[red]✗[/] event chain INVALID — log has been tampered with")
            all_ok = False

    # 2. Evidence manifest.
    manifest_path = run_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        console.print(f"[yellow]![/] evidence manifest not found (runs before v0.5.5 don't have one)")
    elif verify_evidence_manifest(run_dir):
        console.print(f"[green]✓[/] evidence manifest valid (artifacts + event chain match)")
    else:
        console.print(f"[red]✗[/] evidence manifest INVALID — artifacts or event log don't match")
        all_ok = False

    # 3. Ed25519 signatures (optional).
    if public_key is not None:
        if not events_path.is_file():
            console.print(f"[red]✗[/] cannot verify signatures without event log")
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

      1. Reads the vault note and flips ``approved: true`` + ``memory_status: active``
      2. Writes a ``human.approved`` event to the event log
      3. Updates the task status to ``APPROVED``

    Only ``PASSED`` and ``NEEDS_REVIEW`` tasks can be approved. Already-approved
    notes cannot be approved again.
    """
    from acp.vault.approval import approve_vault_note, can_approve

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

    # Approve the note (flips frontmatter) + write the lifecycle event as one
    # atomic-ish unit. If the lifecycle event write fails, revert the vault note
    # to its pre-approval state — the event log is the source of truth, and a
    # modified vault note without a corresponding event is an inconsistent state
    # that must not persist.
    original_note_content = note_path.read_text()
    try:
        fm = approve_vault_note(note_path, approver=approver)
    except PermissionError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"[red]✗[/] approval failed: {exc}")
        raise typer.Exit(code=1)

    # Write the human.approved event — signed with the run's key + dual-written
    # to its durable store, then recompute the evidence manifest so acp verify
    # still passes. Lifecycle evidence must not break the run's verifier.
    try:
        _record_lifecycle_event(
            task_id=task_id,
            run_dir=run_dir,
            event_type=EventType.HUMAN_APPROVED,
            payload={
                "approver": approver or "unknown",
                "vault_note_path": str(note_path),
                "memory_status": fm.memory_status,
            },
        )
    except ACPError as exc:
        _revert_vault_note(note_path, original_note_content)
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        _revert_vault_note(note_path, original_note_content)
        console.print(f"[red]✗[/] lifecycle event write failed: {exc}")
        raise typer.Exit(code=1) from exc

    # Update task status to APPROVED.
    task.status = TaskStatus.APPROVED
    store.save(task)

    console.print(f"[green]✓[/] task {task_id} approved by {approver or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print(f"  memory_status: {fm.memory_status}")
    console.print(f"  event: human.approved written to {store.events_path(task_id)}")
    console.print(f"  evidence manifest refreshed — `acp verify` remains valid")


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
    """
    from acp.vault.approval import reject_vault_note

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

    note_path = vault_root / "tasks" / f"{task_id}.md"
    if not note_path.is_file():
        console.print(f"[red]✗[/] vault note not found: {note_path}")
        raise typer.Exit(code=1)

    # Reject the note (archives it) + write the lifecycle event as one
    # atomic-ish unit. If the lifecycle event write fails, revert the vault note
    # so we don't leave a modified note without a corresponding event.
    original_note_content = note_path.read_text()
    try:
        fm = reject_vault_note(note_path, rejecter=rejecter)
    except PermissionError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"[red]✗[/] rejection failed: {exc}")
        raise typer.Exit(code=1)

    try:
        _record_lifecycle_event(
            task_id=task_id,
            run_dir=run_dir,
            event_type=EventType.HUMAN_REJECTED,
            payload={
                "rejecter": rejecter or "unknown",
                "reason": reason,
                "vault_note_path": str(note_path),
                "memory_status": fm.memory_status,
            },
        )
    except ACPError as exc:
        _revert_vault_note(note_path, original_note_content)
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        _revert_vault_note(note_path, original_note_content)
        console.print(f"[red]✗[/] lifecycle event write failed: {exc}")
        raise typer.Exit(code=1) from exc

    task.status = TaskStatus.ARCHIVED
    store.save(task)

    console.print(f"[green]✓[/] task {task_id} rejected by {rejecter or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print(f"  memory_status: {fm.memory_status}")
    console.print(f"  event: human.rejected written to {store.events_path(task_id)}")
    console.print(f"  evidence manifest refreshed — `acp verify` remains valid")


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
    for task_json in sorted(runs_root.rglob("task.json")):
        try:
            t = Task.model_validate_json(task_json.read_text())
            if status and t.status.value != status:
                continue
            tasks.append((t.task_id, t.status.value, t.repo_name, t.created_at))
        except Exception:  # noqa: BLE001
            pass

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
        False,
        "--force",
        help="Force-remove the worktree even if it has uncommitted changes.",
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
    cfg = load_repo_config(config)
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
