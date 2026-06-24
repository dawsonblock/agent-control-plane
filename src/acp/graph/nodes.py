"""Graph nodes — thin adapters over the M1 worker functions.

Each node:
  1. reads what it needs from ``ACPState``,
  2. calls the *existing* M1/M2 function (gitops, testing, review, reports,
     vault — never re-implemented here),
  3. writes an event via the shared ``EventWriter``,
  4. returns a state patch (a partial dict LangGraph merges in).

This is the M3 refactor: the *same* worker code the linear CLI used, now
driven by the graph instead of a straight-line script. A failed node sets
``status=FAILED`` and the graph routes to the ``failed`` node, which still
writes a report (the spec rule: a failed task produces an evidence report).

A single ``EventWriter`` and ``TaskStore`` are stashed on the nodes via a
``NodeContext`` so every node shares the same run dir + event log. This
avoids threading them through the state (LangGraph state is for data, not
services).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from acp.agents.base import AgentProtocol, write_prompt, write_repair_prompt
from acp.agents.registry import build_agent as _default_build_agent
from acp.config import RepoConfig
from acp.events import EventWriter
from acp.evidence.manifest import write_evidence_config, write_evidence_manifest
from acp.gitops.diff import DiffCapture, capture_diff
from acp.gitops.worktrees import create_worktree, is_clean
from acp.models import EventType, TaskStatus
from acp.reports.writer import write_failure_report, write_report
from acp.review.diff_reviewer import review_diff
from acp.review.gates import evaluate_final_gates, GateOutcome
from acp.store import TaskStore
from acp.testing.parsers import extract_failures
from acp.testing.runner import run_commands, validation_status
from acp.vault.obsidian_writer import write_vault_note


@dataclass
class NodeContext:
    """Shared services threaded through the graph, outside of ACPState.

    ``agent_factory`` is injectable so tests can substitute a controllable
    agent (e.g. one that fixes the failure on a repair attempt). Defaults to
    the registry's ``build_agent``.
    """

    store: TaskStore
    events: EventWriter
    agent_factory: Callable[[RepoConfig], AgentProtocol] = _default_build_agent


# --------------------------------------------------------------------------- #
# Node signature: takes (state, ctx) → patch dict.
# We use functools.partial-style binding in workflow.py to inject ctx.
# --------------------------------------------------------------------------- #


def _finalize_evidence(state: dict[str, Any], ctx: NodeContext) -> str | None:
    """Write the evidence manifest + re-render the report with its hash.

    Called by terminal nodes AFTER the terminal event is written, so the
    manifest's event chain head matches the last event. Returns the manifest
    hash (or ``None`` on failure). Best-effort: failures are logged as
    ``node.failed`` events, not raised.

    The re-rendered report includes the event timeline (full projection of
    the event log) and the manifest hash (evidence binding). For early
    failures (no diff/review), the minimal failure report is re-rendered with
    the final event timeline + manifest hash so it is a true projection of the
    final event log, not a snapshot from before the terminal event.

    Also persists the run's evidence config (signing key / durable store /
    public key paths) as a sidecar so post-run lifecycle commands can recover
    the same signing key + durable store the run used.
    """
    try:
        run_dir = ctx.store.run_dir(state["task_id"])
        _, manifest_hash = write_evidence_manifest(
            run_dir=run_dir,
            events_writer=ctx.events,
        )
        # Persist the evidence config sidecar so approve/reject can recover the
        # run's signing key + durable store (lifecycle events must be signed
        # with the same key and dual-written to the same SQLite index).
        cfg = state.get("config")
        evidence_cfg = getattr(cfg, "evidence", None)
        if evidence_cfg is not None:
            write_evidence_config(
                run_dir,
                signing_key_path=evidence_cfg.signing_key_path,
                durable_store=evidence_cfg.durable_store,
                public_key_path=evidence_cfg.public_key_path,
            )

        # Re-render the report with the manifest hash + event timeline so
        # the report ↔ evidence binding is verifiable and the report is a
        # true projection of the event log.
        report_path = state.get("report_path")
        review = state.get("review_result")
        events = ctx.events.read_all()
        if report_path and review is not None and state.get("diff") is not None:
            write_report(
                task=state["task"],
                command_results=state.get("command_results", []),
                review=review,
                diff=state["diff"],
                artifact_dir=state["artifacts_dir"],
                agent_result=state.get("agent_result"),
                repair_history=state.get("repair_history", []),
                gate_result=state.get("gate_result"),
                manifest_hash=manifest_hash,
                events=events,
            )
        elif report_path and review is None and state.get("diff") is None:
            # Early failure: re-render the minimal failure report with the
            # final event timeline + manifest hash. The first render (in
            # failed_node) happened before report.written/task.failed were
            # appended, so its timeline was stale.
            write_failure_report(
                task=state["task"],
                error=state.get("error", "unknown"),
                artifact_dir=state["artifacts_dir"],
                manifest_hash=manifest_hash,
                events=events,
            )
        return manifest_hash
    except Exception as exc:  # noqa: BLE001
        ctx.events.write(
            EventType.NODE_FAILED,
            {"node": "finalize_evidence.manifest", "message": str(exc)},
        )
        return None


def create_task(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    repo_path = cfg.repo.path
    task_id = ctx.store.next_task_id(repo_path=repo_path)
    # The EventWriter was constructed with a placeholder id; point it at the
    # real run dir now that we know the task id.
    ctx.events.relocate(task_id, ctx.store.run_dir(task_id))
    task = ctx.store.create(
        task_id=task_id,
        repo_name=cfg.repo.name,
        repo_path=repo_path,
        base_branch=cfg.repo.default_branch,
        user_request=state["user_request"],
    )
    ctx.events.write(EventType.TASK_CREATED, {"request": state["user_request"]})
    return {
        "task_id": task_id,
        "task": task,
        "status": task.status,
        "repo_path": repo_path,
        "artifacts_dir": ctx.store.artifacts_dir(task_id),
    }


def check_repo(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    repo_path = state["repo_path"]
    clean = is_clean(repo_path)
    ctx.events.write(
        EventType.REPO_CHECKED,
        {"repo_path": str(repo_path), "clean": clean},
    )
    if not clean:
        # Set status=FAILED so the graph routes to `failed_node`, which writes
        # the single terminal event. Non-terminal nodes never write terminal
        # events (task.failed / task.completed / task.needs_review) — only the
        # terminal nodes do. This prevents duplicate terminal events.
        task = state["task"]
        task.status = TaskStatus.FAILED
        ctx.store.save(task)
        return {"status": TaskStatus.FAILED, "error": f"repo dirty: {repo_path}"}
    return {}


def create_worktree_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    task = state["task"]
    try:
        worktree_path, base_sha = create_worktree(
            repo_path=state["repo_path"],
            base_branch=cfg.repo.default_branch,
            branch_name=task.task_branch,
            target_path=ctx.store.worktree_path(state["task_id"]),
        )
        task.base_commit_sha = base_sha
        ctx.store.save(task)
    except Exception as exc:  # noqa: BLE001
        # Write a node.failed event (NOT a terminal task.failed) — the
        # failed_node terminal node writes the single terminal event.
        ctx.events.write(
            EventType.NODE_FAILED,
            {"node": "create_worktree", "reason": "worktree creation failed", "detail": str(exc)},
        )
        task.status = TaskStatus.FAILED
        ctx.store.save(task)
        return {"status": TaskStatus.FAILED, "error": str(exc)}
    task.status = TaskStatus.WORKTREE_CREATED
    ctx.store.save(task)
    ctx.events.write(
        EventType.WORKTREE_CREATED,
        {"branch": task.task_branch, "worktree_path": str(worktree_path), "base_commit_sha": task.base_commit_sha},
    )
    return {"worktree_path": worktree_path}


def build_context_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """M1/M3: build the prompt only. M6 will prepend a Haystack context bundle."""
    cfg = state["config"]
    prompt_path = write_prompt(
        user_request=state["user_request"],
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
        repo_config=cfg,
        context_bundle_path=None,
    )
    ctx.events.write(
        EventType.CONTEXT_BUILT,
        {"prompt_path": str(prompt_path), "haystack": False},
    )
    return {"prompt_path": prompt_path}


def run_agent_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    task = state["task"]
    agent = ctx.agent_factory(cfg)
    task.status = TaskStatus.EXECUTING
    ctx.store.save(task)
    ctx.events.write(
        EventType.AGENT_STARTED,
        {"agent": agent.name, "timeout_seconds": cfg.agent.timeout_seconds},
    )
    agent_result = agent.run(
        prompt_path=state["prompt_path"],
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
        timeout_seconds=cfg.agent.timeout_seconds,
    )
    ctx.events.write(
        EventType.AGENT_FINISHED,
        {
            "agent": agent_result.agent_name,
            "exit_code": agent_result.exit_code,
            "summary": agent_result.summary,
        },
    )
    return {"agent_result": agent_result}


def run_tests_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    state["task"].status = TaskStatus.TESTING
    ctx.store.save(state["task"])
    cmd_timeout = cfg.commands.timeout_seconds or cfg.agent.timeout_seconds
    command_results = run_commands(
        repo_config=cfg,
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
        timeout_seconds=cmd_timeout,
        event_writer=ctx.events,
    )

    # If repair attempts have been used, tests still fail, and the cap is
    # reached, write repair.exhausted — meaning "another repair was needed
    # but the cap blocked it." This is distinct from repair.attempted (which
    # is written for every attempt, including the last one).
    from acp.testing.runner import validation_passed, validation_ran
    attempts = int(state.get("repair_attempts", 0))
    max_attempts = cfg.agent.max_repair_attempts
    has_failures = validation_ran(command_results) and not validation_passed(command_results)
    if has_failures and attempts >= max_attempts and attempts > 0:
        ctx.events.write(
            EventType.REPAIR_EXHAUSTED,
            {
                "attempt": attempts,
                "max_attempts": max_attempts,
                "reason": "tests still failing after cap reached",
            },
        )

    return {"command_results": command_results}


def capture_diff_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    task = state["task"]
    base_sha = task.base_commit_sha or cfg.repo.default_branch
    diff: DiffCapture = capture_diff(
        worktree_path=state["worktree_path"],
        base_branch=cfg.repo.default_branch,
        artifacts_dir=state["artifacts_dir"],
        base_commit_sha=base_sha or None,
    )
    ctx.events.write(
        EventType.DIFF_CAPTURED,
        {
            "files": len(diff.changed_files),
            "insertions": diff.insertions,
            "deletions": diff.deletions,
        },
    )
    return {"diff": diff}


def review_diff_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    state["task"].status = TaskStatus.REVIEWING
    ctx.store.save(state["task"])
    review = review_diff(
        diff=state["diff"],
        command_results=state["command_results"],
        repo_config=cfg,
        artifacts_dir=state["artifacts_dir"],
    )
    ctx.events.write(
        EventType.REVIEW_COMPLETED,
        {
            "risk": review.risk.value,
            "recommendation": review.recommendation.value,
            "concerns": len(review.concerns),
        },
    )
    return {"review_result": review}


def write_report_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    task = state["task"]
    review = state.get("review_result")
    agent_result = state.get("agent_result")
    command_results = state.get("command_results", [])
    diff = state.get("diff", DiffCapture(patch="", stat="", changed_files=[], insertions=0, deletions=0))

    # Evaluate gates directly — GateResult is now the single truth object.
    gate_result = evaluate_final_gates(
        agent_exit_code=agent_result.exit_code if agent_result else None,
        command_results=command_results,
        review_result=review,
        changed_files=diff.changed_files,
    )
    task.status = (
        TaskStatus.PASSED if gate_result.outcome == GateOutcome.PASSED
        else TaskStatus.NEEDS_REVIEW if gate_result.outcome == GateOutcome.NEEDS_REVIEW
        else TaskStatus.FAILED
    )
    ctx.store.save(task)

    report_path = write_report(
        task=task,
        command_results=command_results,
        review=review,
        diff=diff,
        artifact_dir=state["artifacts_dir"],
        agent_result=agent_result,
        repair_history=state.get("repair_history", []),
        gate_result=gate_result,
    )
    ctx.events.write(EventType.REPORT_WRITTEN, {"report_path": str(report_path)})

    # Write vault note for ALL statuses (PASSED, FAILED, NEEDS_REVIEW).
    vault_note_path = None
    if review is not None:
        try:
            vault_note_path = write_vault_note(
                report_body=report_path.read_text(),
                task=task,
                review=review,
                diff=diff,
                vault_root=state["vault_root"],
            )
            ctx.events.write(
                EventType.VAULT_NOTE_WRITTEN,
                {"vault_note_path": str(vault_note_path)},
            )
        except Exception as exc:  # noqa: BLE001
            # Vault write failure must be visible — write a node.failed event.
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "write_report_node.vault", "message": str(exc)},
            )
            # If the task would have been PASSED, degrade to NEEDS_REVIEW
            # because the review surface (vault note) was not written. The
            # report was already rendered with status=PASSED, so we must
            # re-render it from the downgraded state — otherwise the on-disk
            # final_report.md would say "passed" while the terminal status is
            # "needs_review". The GateResult is downgraded too so the Gate
            # Summary section stays consistent with the final outcome.
            if task.status == TaskStatus.PASSED:
                task.status = TaskStatus.NEEDS_REVIEW
                ctx.store.save(task)
                gate_result.outcome = GateOutcome.NEEDS_REVIEW
                gate_result.reasons.append(
                    "Vault note write failed — review surface not written; "
                    "downgraded from PASSED to NEEDS_REVIEW."
                )
                report_path = write_report(
                    task=task,
                    command_results=command_results,
                    review=review,
                    diff=diff,
                    artifact_dir=state["artifacts_dir"],
                    agent_result=agent_result,
                    repair_history=state.get("repair_history", []),
                    gate_result=gate_result,
                )
                ctx.events.write(
                    EventType.REPORT_WRITTEN,
                    {"report_path": str(report_path), "reason": "vault-failure downgrade"},
                )

    # The evidence manifest is written by the terminal nodes (after the
    # terminal event) so the event chain head in the manifest matches the
    # last event. The terminal node then re-renders the report with the
    # manifest hash.

    return {
        "report_path": report_path,
        "vault_note_path": vault_note_path,
        "status": task.status,
        "gate_result": gate_result,
    }


def done_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Terminal success node. Writes task.completed with gate-derived validation fields."""
    task = state["task"]
    gate_result = state.get("gate_result")
    ctx.events.write(
        EventType.TASK_COMPLETED,
        {
            "status": task.status.value,
            "validation_commands_ran": gate_result.validation_commands_ran if gate_result else 0,
            "validation_commands_failed": gate_result.validation_commands_failed if gate_result else 0,
            "validation_status": validation_status(state.get("command_results", [])),
            "recommendation": state["review_result"].recommendation.value if state.get("review_result") else "unknown",
        },
    )
    manifest_hash = _finalize_evidence(state, ctx)
    return {"status": task.status, "manifest_hash": manifest_hash}


def failed_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Terminal failure node — the ONLY node that writes ``TASK_FAILED``.

    If ``write_report_node`` already wrote the report (normal graph path),
    this node only writes the terminal ``TASK_FAILED`` event. If we failed
    before ``write_report_node`` ran (e.g. dirty repo, worktree error, node
    crash), we write a minimal failure report so the evidence trail is never
    empty — even when there's no diff or review to render. When a diff +
    review exist (mid-run failure), we write the full report instead.
    """
    task = state["task"]
    report_path = state.get("report_path")
    vault_note_path = state.get("vault_note_path")
    error = state.get("error", "unknown")

    # Only write report/vault note if NOT already written by write_report_node.
    if report_path is None:
        try:
            if state.get("diff") is not None and state.get("review_result") is not None:
                # Mid-run failure: we have a diff and review → full report.
                report_path = write_report(
                    task=task,
                    command_results=state.get("command_results", []),
                    review=state["review_result"],
                    diff=state["diff"],
                    artifact_dir=state["artifacts_dir"],
                    agent_result=state.get("agent_result"),
                    repair_history=state.get("repair_history", []),
                    gate_result=state.get("gate_result"),
                    events=ctx.events.read_all(),
                )
            else:
                # Early failure: no diff/review → minimal failure report.
                report_path = write_failure_report(
                    task=task,
                    error=error,
                    artifact_dir=state["artifacts_dir"],
                    events=ctx.events.read_all(),
                )
            ctx.events.write(EventType.REPORT_WRITTEN, {"report_path": str(report_path)})

            # Write vault note if we have a review (needed for frontmatter).
            if state.get("review_result") is not None and state.get("diff") is not None:
                vault_note_path = write_vault_note(
                    report_body=report_path.read_text(),
                    task=task,
                    review=state["review_result"],
                    diff=state["diff"],
                    vault_root=state["vault_root"],
                )
                ctx.events.write(
                    EventType.VAULT_NOTE_WRITTEN,
                    {"vault_note_path": str(vault_note_path)},
                )
        except Exception as exc:  # noqa: BLE001
            # Evidence write failure is visible — don't mask the real failure.
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "failed_node.evidence", "message": str(exc)},
            )

    task.status = TaskStatus.FAILED
    ctx.store.save(task)
    gate_result = state.get("gate_result")
    ctx.events.write(
        EventType.TASK_FAILED,
        {
            "status": task.status.value,
            "error": error,
            "validation_commands_ran": gate_result.validation_commands_ran if gate_result else 0,
            "validation_commands_failed": gate_result.validation_commands_failed if gate_result else 0,
            "validation_status": validation_status(state.get("command_results", [])),
        },
    )
    # Write the evidence manifest AFTER the terminal event so the chain head
    # is current. Best-effort — don't mask the real failure. Surface the
    # locally-written report path into state so _finalize_evidence can re-render
    # the early-failure report with the final event timeline + manifest hash.
    if report_path is not None:
        state["report_path"] = report_path
    manifest_hash = _finalize_evidence(state, ctx)
    return {"status": TaskStatus.FAILED, "report_path": report_path, "vault_note_path": vault_note_path, "manifest_hash": manifest_hash}


def needs_review_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Terminal node for tasks that completed but need human review.

    ``write_report_node`` already wrote the report; this node only writes
    the terminal ``TASK_NEEDS_REVIEW`` event.
    """
    task = state["task"]
    task.status = TaskStatus.NEEDS_REVIEW
    ctx.store.save(task)
    gate_result = state.get("gate_result")
    ctx.events.write(
        EventType.TASK_NEEDS_REVIEW,
        {
            "status": task.status.value,
            "report_path": str(state.get("report_path", "")),
            "validation_commands_ran": gate_result.validation_commands_ran if gate_result else 0,
            "validation_commands_failed": gate_result.validation_commands_failed if gate_result else 0,
            "validation_status": validation_status(state.get("command_results", [])),
            "recommendation": state["review_result"].recommendation.value if state.get("review_result") else "unknown",
        },
    )
    return {
        "status": TaskStatus.NEEDS_REVIEW,
        "report_path": state.get("report_path"),
        "vault_note_path": state.get("vault_note_path"),
        "manifest_hash": _finalize_evidence(state, ctx),
    }


# --------------------------------------------------------------------------- #
# M4 repair loop nodes.
#
# Routed to from run_tests when tests fail and attempts remain. The loop is:
#   repair_plan  → build a prompt from the failing commands' output
#   run_repair   → run the agent against that prompt
#   run_tests    → re-run the commands (the same node as the initial run)
# The router in workflow.py caps attempts at config.agent.max_repair_attempts,
# so the graph cannot loop forever.
# --------------------------------------------------------------------------- #


def repair_plan_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Build a repair prompt from the most recent command failures.

    Increments ``repair_attempts`` and records a history entry. Every repair
    attempt is logged as ``repair.attempted`` — including the last one. The
    ``repair.exhausted`` event is written separately by the router (in
    ``_route_after_tests``) when tests still fail after the cap is reached,
    so "exhausted" means "another repair was needed but the cap blocked it,"
    not "this was the last allowed attempt."
    """
    cfg = state["config"]
    task = state["task"]
    attempts = int(state.get("repair_attempts", 0)) + 1
    max_attempts = cfg.agent.max_repair_attempts

    task.status = TaskStatus.REPAIRING
    ctx.store.save(task)

    failures = extract_failures(state.get("command_results", []))
    prompt_path = write_repair_prompt(
        original_request=state["user_request"],
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
        repo_config=cfg,
        failures=failures,
        attempt=attempts,
        max_attempts=max_attempts,
    )

    ctx.events.write(
        EventType.REPAIR_ATTEMPTED,
        {
            "attempt": attempts,
            "max_attempts": max_attempts,
            "failures": len(failures),
            "prompt_path": str(prompt_path),
        },
    )

    history = list(state.get("repair_history", []))
    history.append({"attempt": attempts, "prompt_path": str(prompt_path)})

    return {
        "repair_attempts": attempts,
        "repair_history": history,
        "prompt_path": prompt_path,
    }


def run_repair_agent_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Run the configured agent against the repair prompt.

    Reuses the same agent registry + run contract as the initial ``run_agent``
    node — only the prompt differs. Output lands in the standard
    ``agent_stdout.txt`` / ``agent_stderr.txt`` (overwritten per attempt; the
    distinct ``repair_prompt_<n>.txt`` artifacts preserve the per-attempt
    evidence trail).
    """
    cfg = state["config"]
    agent = ctx.agent_factory(cfg)
    ctx.events.write(
        EventType.AGENT_STARTED,
        {
            "agent": agent.name,
            "phase": "repair",
            "attempt": state.get("repair_attempts", 1),
            "timeout_seconds": cfg.agent.timeout_seconds,
        },
    )
    agent_result = agent.run(
        prompt_path=state["prompt_path"],
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
        timeout_seconds=cfg.agent.timeout_seconds,
    )
    ctx.events.write(
        EventType.AGENT_FINISHED,
        {
            "agent": agent_result.agent_name,
            "phase": "repair",
            "attempt": state.get("repair_attempts", 1),
            "exit_code": agent_result.exit_code,
            "summary": agent_result.summary,
        },
    )
    return {"agent_result": agent_result}
