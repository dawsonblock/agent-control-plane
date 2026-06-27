"""The legacy linear evidence loop — kept for tests, NOT for production use.

ACP's production engine is the LangGraph workflow in ``acp.graph.workflow``;
``acp run`` uses the graph exclusively. This module preserves the original
linear ``EvidenceLoop`` (M1) so the graph-equivalence and agent-swap tests
can compare the two engines against each other. It is deliberately
quarantined out of the production CLI module (``acp.cli``) so it cannot drift
into the product path — one engine only.

Do not add new features here. If the graph has proven equivalent for long
enough, delete this module and the tests that depend on it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from acp.agents.base import AgentProtocol, write_prompt
from acp.agents.registry import build_agent
from acp.config import RepoConfig
from acp.errors import RepoDirtyError, WorktreeError
from acp.events import EventWriter
from acp.gitops.diff import DiffCapture, capture_diff
from acp.gitops.worktrees import create_worktree, is_clean, remove_worktree
from acp.models import EventType, TaskStatus
from acp.reports.writer import write_report
from acp.review.diff_reviewer import review_diff
from acp.review.gates import GateOutcome, evaluate_final_gates
from acp.store import TaskStore
from acp.testing.runner import run_commands, validation_passed, validation_status
from acp.vault.obsidian_writer import write_vault_note

console = Console()


# --------------------------------------------------------------------------- #
# Agent factory — delegates to the registry (the single dispatch point).
# --------------------------------------------------------------------------- #


def _build_agent(config: RepoConfig) -> AgentProtocol:
    """Build the agent the repo config selected, via the registry."""
    return build_agent(config)


# --------------------------------------------------------------------------- #
# The evidence loop
# --------------------------------------------------------------------------- #


@dataclass
class LoopResult:
    """What ``EvidenceLoop.run`` produced, for tests to inspect."""

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
            {
                "branch": task.task_branch,
                "worktree_path": str(worktree_path),
                "base_commit_sha": task.base_commit_sha,
            },
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
        console.print(f"[green]✓[/] agent finished (exit {agent_result.exit_code})")

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
        tests_pass = validation_passed(command_results)
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
            f"[yellow]![/] review: risk={review.risk.value} rec={review.recommendation.value}"
        )

        # 9. Compute final status via GateResult (single source of truth). #
        gate_result = evaluate_final_gates(
            agent_exit_code=agent_result.exit_code if agent_result else None,
            command_results=command_results,
            review_result=review,
            changed_files=diff.changed_files,
        )
        status = (
            TaskStatus.PASSED
            if gate_result.outcome == GateOutcome.PASSED
            else TaskStatus.NEEDS_REVIEW
            if gate_result.outcome == GateOutcome.NEEDS_REVIEW
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
