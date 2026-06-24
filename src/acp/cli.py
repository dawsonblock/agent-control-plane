"""ACP command-line interface.

``acp run`` is the M1 entry point: it runs one coding task in an isolated
git worktree, captures everything, reviews the diff, and writes an evidence
report + Obsidian note. The orchestration lives in ``EvidenceLoop`` so the
M3 LangGraph refactor can call the same steps as nodes without rewriting
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console

from acp.agents.base import AgentProtocol, write_prompt
from acp.agents.registry import build_agent
from acp.config import RepoConfig, load_repo_config
from acp.errors import ACPError, RepoDirtyError, WorktreeError
from acp.events import EventWriter
from acp.gitops.diff import DiffCapture, capture_diff
from acp.gitops.worktrees import create_worktree, is_clean, remove_worktree
from acp.models import EventType, Task, TaskStatus
from acp.reports.writer import write_report
from acp.review.diff_reviewer import review_diff
from acp.review.gates import evaluate_final_gates, GateOutcome
from acp.store import TaskStore
from acp.testing.runner import all_passed, run_commands, validation_status
from acp.vault.obsidian_writer import write_vault_note

app = typer.Typer(
    name="acp",
    help="agent-control-plane: safely run one coding task, capture everything.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# --------------------------------------------------------------------------- #
# Agent factory — delegates to the registry (the single dispatch point).
# M2 replaced M1's inline _build_agent; the EvidenceLoop now uses this by
# default, and tests can inject a custom factory if needed.
# --------------------------------------------------------------------------- #

def _build_agent(config: RepoConfig) -> AgentProtocol:
    """Build the agent the repo config selected, via the registry."""
    return build_agent(config)


# --------------------------------------------------------------------------- #
# The evidence loop
# --------------------------------------------------------------------------- #

@dataclass
class LoopResult:
    """What ``EvidenceLoop.run`` produced, for the CLI / tests to inspect."""

    task_id: str
    status: TaskStatus
    run_dir: Path
    report_path: Path
    vault_note_path: Path


class EvidenceLoop:
    """Orchestrates the linear M1 evidence loop.

    Each step writes an event. Failures are caught at step boundaries; even on
    failure we still try to write a report (the spec rule: a failed task still
    produces an evidence report). The dirty-repo pre-check is the one case
    where we fail *before* creating a worktree.
    """

    def __init__(
        self,
        *,
        config: RepoConfig,
        user_request: str,
        store: TaskStore | None = None,
        vault_root: Path | str | None = None,
        agent_factory: Callable[[RepoConfig], AgentProtocol] = _build_agent,
        keep_worktree: bool = True,
    ) -> None:
        self.config = config
        self.user_request = user_request
        self.store = store or TaskStore()
        self.vault_root = Path(vault_root or "vault").resolve()
        self.agent_factory = agent_factory
        self.keep_worktree = keep_worktree

    def run(self) -> LoopResult:
        cfg = self.config
        repo_path = cfg.repo.path

        # 1. Task id + run dir + initial event ------------------------------ #
        # Pass repo_path so the id (and thus the branch name) can't collide
        # with an existing agent/task_* branch in this repo.
        task_id = self.store.next_task_id(repo_path=repo_path)
        task = self.store.create(
            task_id=task_id,
            repo_name=cfg.repo.name,
            repo_path=repo_path,
            base_branch=cfg.repo.default_branch,
            user_request=self.user_request,
        )
        events = EventWriter(task_id, self.store.run_dir(task_id))
        events.write(EventType.TASK_CREATED, {"request": self.user_request})

        # 2. Repo cleanliness pre-check ------------------------------------- #
        if not is_clean(repo_path):
            events.write(
                EventType.TASK_FAILED,
                {"reason": "repo dirty", "repo_path": str(repo_path)},
            )
            task.status = TaskStatus.FAILED
            self.store.save(task)
            console.print(
                f"[red]✗[/] repo is dirty; refusing to start: {repo_path}\n"
                f"  (no worktree created — see [[worktree-safety]])"
            )
            # Dirty-repo is a pre-worktree failure: nothing to report on.
            raise RepoDirtyError(f"repo is dirty; refusing to start: {repo_path}")
        events.write(
            EventType.REPO_CHECKED,
            {"repo_path": str(repo_path), "clean": True},
        )

        # 3. Worktree ------------------------------------------------------- #
        try:
            worktree_path, base_sha = create_worktree(
                repo_path=repo_path,
                base_branch=cfg.repo.default_branch,
                branch_name=task.task_branch,
                target_path=self.store.worktree_path(task_id),
            )
            task.base_commit_sha = base_sha
            self.store.save(task)
        except Exception as exc:  # noqa: BLE001
            events.write(
                EventType.TASK_FAILED,
                {"reason": "worktree creation failed", "detail": str(exc)},
            )
            task.status = TaskStatus.FAILED
            self.store.save(task)
            console.print(f"[red]✗[/] worktree creation failed: {exc}")
            raise WorktreeError(f"worktree creation failed: {exc}") from exc

        task.status = TaskStatus.WORKTREE_CREATED
        self.store.save(task)
        events.write(
            EventType.WORKTREE_CREATED,
            {"branch": task.task_branch, "worktree_path": str(worktree_path), "base_commit_sha": task.base_commit_sha},
        )
        console.print(f"[green]✓[/] worktree: {worktree_path}")

        artifacts = self.store.artifacts_dir(task_id)

        # From here on, even if something fails we have a diff to report on. #
        try:
            return self._run_after_worktree(task, events, worktree_path, artifacts)
        finally:
            if not self.keep_worktree:
                try:
                    remove_worktree(repo_path, worktree_path)
                except Exception:  # noqa: BLE001
                    pass  # cleanup is best-effort

    def _run_after_worktree(
        self,
        task,
        events: EventWriter,
        worktree_path: Path,
        artifacts: Path,
    ) -> LoopResult:
        cfg = self.config

        # 4. Build context (M1: just the prompt; M6 will add a bundle) ----- #
        prompt_path = write_prompt(
            user_request=task.user_request,
            worktree_path=worktree_path,
            artifact_dir=artifacts,
            repo_config=cfg,
        )
        events.write(
            EventType.CONTEXT_BUILT,
            {"prompt_path": str(prompt_path), "haystack": False},
        )

        # 5. Run agent ----------------------------------------------------- #
        agent = self.agent_factory(cfg)
        task.status = TaskStatus.EXECUTING
        self.store.save(task)
        events.write(
            EventType.AGENT_STARTED,
            {"agent": agent.name, "timeout_seconds": cfg.agent.timeout_seconds},
        )
        agent_result = agent.run(
            prompt_path=prompt_path,
            worktree_path=worktree_path,
            artifact_dir=artifacts,
            timeout_seconds=cfg.agent.timeout_seconds,
        )
        events.write(
            EventType.AGENT_FINISHED,
            {
                "agent": agent_result.agent_name,
                "exit_code": agent_result.exit_code,
                "summary": agent_result.summary,
            },
        )
        console.print(
            f"[green]✓[/] agent finished (exit {agent_result.exit_code})"
        )

        # 6. Run configured commands --------------------------------------- #
        task.status = TaskStatus.TESTING
        self.store.save(task)
        cmd_timeout = cfg.commands.timeout_seconds or cfg.agent.timeout_seconds
        command_results = run_commands(
            repo_config=cfg,
            worktree_path=worktree_path,
            artifact_dir=artifacts,
            timeout_seconds=cmd_timeout,
            event_writer=events,
        )
        tests_pass = all_passed(command_results)
        console.print(
            f"[{'green' if tests_pass else 'red'}]{'✓' if tests_pass else '✗'}[/] "
            f"commands: {sum(1 for r in command_results if not r.skipped)} ran"
        )

        # 7. Capture diff -------------------------------------------------- #
        diff: DiffCapture = capture_diff(
            worktree_path=worktree_path,
            base_branch=cfg.repo.default_branch,
            artifacts_dir=artifacts,
            base_commit_sha=task.base_commit_sha or None,
        )
        events.write(
            EventType.DIFF_CAPTURED,
            {
                "files": len(diff.changed_files),
                "insertions": diff.insertions,
                "deletions": diff.deletions,
            },
        )

        # 8. Review -------------------------------------------------------- #
        task.status = TaskStatus.REVIEWING
        self.store.save(task)
        review = review_diff(
            diff=diff,
            command_results=command_results,
            repo_config=cfg,
            artifacts_dir=artifacts,
        )
        events.write(
            EventType.REVIEW_COMPLETED,
            {
                "risk": review.risk.value,
                "recommendation": review.recommendation.value,
                "concerns": len(review.concerns),
            },
        )
        console.print(
            f"[yellow]![/] review: risk={review.risk.value} "
            f"rec={review.recommendation.value}"
        )

        # 9. Compute final status via GateResult (single source of truth). #
        gate_result = evaluate_final_gates(
            agent_exit_code=agent_result.exit_code if agent_result else None,
            command_results=command_results,
            review_result=review,
            changed_files=diff.changed_files,
        )
        status = (
            TaskStatus.PASSED if gate_result.outcome == GateOutcome.PASSED
            else TaskStatus.NEEDS_REVIEW if gate_result.outcome == GateOutcome.NEEDS_REVIEW
            else TaskStatus.FAILED
        )
        task.status = status
        self.store.save(task)

        # 10. Write report (with GateResult for accurate gate summary). --- #
        report_path = write_report(
            task=task,
            command_results=command_results,
            review=review,
            diff=diff,
            artifact_dir=artifacts,
            agent_result=agent_result,
            gate_result=gate_result,
            events=events.read_all(),
        )
        events.write(
            EventType.REPORT_WRITTEN,
            {"report_path": str(report_path)},
        )

        # 11. Write vault note -------------------------------------------- #
        vault_note_path = write_vault_note(
            report_body=report_path.read_text(),
            task=task,
            review=review,
            diff=diff,
            vault_root=self.vault_root,
        )
        events.write(
            EventType.VAULT_NOTE_WRITTEN,
            {"vault_note_path": str(vault_note_path)},
        )

        # 11b. (Evidence manifest is written after the terminal event below
        #      so the event chain head in the manifest matches the last event.)

        # 12. Final event -------------------------------------------------- #
        status = task.status
        if status == TaskStatus.PASSED:
            final_event = EventType.TASK_COMPLETED
        elif status == TaskStatus.NEEDS_REVIEW:
            final_event = EventType.TASK_NEEDS_REVIEW
        else:
            final_event = EventType.TASK_FAILED
        events.write(
            final_event,
            {
                "status": status.value,
                "validation_commands_ran": gate_result.validation_commands_ran,
                "validation_commands_failed": gate_result.validation_commands_failed,
                "validation_status": validation_status(command_results),
                "recommendation": review.recommendation.value,
            },
        )

        # 12b. Write evidence manifest + re-render report with manifest hash. #
        #      After the terminal event so the chain head is current.
        manifest_hash = None
        try:
            from acp.evidence.manifest import write_evidence_manifest
            _, manifest_hash = write_evidence_manifest(
                run_dir=self.store.run_dir(task.task_id),
                events_writer=events,
            )
            report_path = write_report(
                task=task,
                command_results=command_results,
                review=review,
                diff=diff,
                artifact_dir=artifacts,
                agent_result=agent_result,
                gate_result=gate_result,
                manifest_hash=manifest_hash,
                events=events.read_all(),
            )
        except Exception as exc:  # noqa: BLE001
            events.write(
                EventType.NODE_FAILED,
                {"node": "evidence_loop.manifest", "message": str(exc)},
            )

        console.print(f"[green]✓ report:[/] {report_path}")
        console.print(f"[green]✓ vault note:[/] {vault_note_path}")
        console.print(
            f"\n[dim]Task {task.task_id} → {status.value}. "
            f"Review the vault note and set approved: true to promote memory.[/]"
        )
        return LoopResult(
            task_id=task.task_id,
            status=task.status,
            run_dir=self.store.run_dir(task.task_id),
            report_path=report_path,
            vault_note_path=vault_note_path,
        )


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

    # Approve the note (flips frontmatter).
    try:
        fm = approve_vault_note(note_path, approver=approver)
    except PermissionError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"[red]✗[/] approval failed: {exc}")
        raise typer.Exit(code=1)

    # Write the human.approved event.
    events = EventWriter(task_id, run_dir)
    events.write(
        EventType.HUMAN_APPROVED,
        {
            "approver": approver or "unknown",
            "vault_note_path": str(note_path),
            "memory_status": fm.memory_status,
        },
    )

    # Update task status to APPROVED.
    task.status = TaskStatus.APPROVED
    store.save(task)

    console.print(f"[green]✓[/] task {task_id} approved by {approver or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print(f"  memory_status: {fm.memory_status}")
    console.print(f"  event: human.approved written to {store.events_path(task_id)}")


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

    try:
        fm = reject_vault_note(note_path, rejecter=rejecter)
    except PermissionError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"[red]✗[/] rejection failed: {exc}")
        raise typer.Exit(code=1)

    events = EventWriter(task_id, run_dir)
    events.write(
        EventType.HUMAN_REJECTED,
        {
            "rejecter": rejecter or "unknown",
            "reason": reason,
            "vault_note_path": str(note_path),
            "memory_status": fm.memory_status,
        },
    )

    task.status = TaskStatus.ARCHIVED
    store.save(task)

    console.print(f"[green]✓[/] task {task_id} rejected by {rejecter or 'unknown'}")
    console.print(f"  vault note: {note_path}")
    console.print(f"  memory_status: {fm.memory_status}")
    console.print(f"  event: human.rejected written to {store.events_path(task_id)}")


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
    cfg = load_repo_config(config)
    repo_path = cfg.repo.path
    store = TaskStore(runs_root=runs_root)
    task_branch = f"agent/{task_id}"
    worktree_path = store.worktree_path(task_id)

    cleaned = []

    # 1. Remove the worktree.
    if worktree_path.exists():
        try:
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
