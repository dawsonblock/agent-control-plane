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
from acp.executor.sbx import SbxExecutor, SbxNotInstalledError
from acp.evidence.manifest import (
    build_evidence_manifest,
    compute_artifact_content_hash,
    compute_evidence_config_hash,
    compute_report_hash,
    compute_task_json_hash,
    write_evidence_config,
    write_evidence_manifest,
)
from acp.gitops.diff import DiffCapture, capture_diff, capture_diff_from_remote
from acp.gitops.worktrees import create_worktree, create_worktree_from_ref, is_clean, remove_worktree
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
    """Write the evidence manifest, bind it to the signed event log, re-render.

    Called by terminal nodes AFTER the terminal event is written. The sequence:

    1. Compute artifact_content_hash + task_json_hash (stable across manifest
       rewrites — they don't depend on the event chain).
    2. Write ``evidence_manifest.json`` (artifact hashes + event chain head).
    3. Write an ``evidence.finalized`` event containing artifact_content_hash,
       task_json_hash, artifact count, and final status. This event is part
       of the hash-chained, optionally signed event log — so the artifact
       manifest + task metadata are cryptographically bound to the signed
       evidence. Tampering with any artifact or task.json field (other than
       status during a lifecycle transition) breaks the signed event.
    4. Rewrite the manifest so its chain head includes the finalized event.
    5. Re-render the report with the final manifest hash + event timeline
       (which now includes evidence.finalized).
    6. Write an ``evidence.report_bound`` event containing the report_hash.
       This binds the human-facing report to the signed event log. Tampering
       with or deleting the report breaks this signed event.
    7. Rewrite the manifest so its chain head includes the report_bound event.

    For early failures (no diff/review), the minimal failure report is
    re-rendered with the final event timeline + manifest hash.

    Also persists the run's evidence config as a sidecar so post-run lifecycle
    commands can recover the same signing key + durable store + durable mode.
    """
    try:
        run_dir = ctx.store.run_dir(state["task_id"])

        # 1. Compute stable hashes — these don't change when the event chain
        # grows, so they can be verified later even though the manifest is
        # rewritten after the finalize event.
        artifact_content_hash = compute_artifact_content_hash(run_dir)
        task_json_hash = compute_task_json_hash(run_dir)

        # 2. Write the initial manifest (artifact hashes + current chain head).
        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=ctx.events)
        manifest_hash = manifest["manifest_hash"]
        artifact_count = len(manifest.get("artifacts", {}))

        # 3. Determine final status from the state.
        final_status = str(state.get("status", "unknown"))

        # 3b. Persist the evidence config sidecar (including durable_mode)
        # BEFORE writing evidence.finalized, so we can compute its hash and
        # bind it to the signed event. This prevents an operator from
        # silently downgrading durable_mode after finalize.
        cfg = state.get("config")
        evidence_cfg = getattr(cfg, "evidence", None)
        if evidence_cfg is not None:
            write_evidence_config(
                run_dir,
                signing_key_path=evidence_cfg.signing_key_path,
                durable_store=evidence_cfg.durable_store,
                public_key_path=evidence_cfg.public_key_path,
                durable_mode=getattr(evidence_cfg, "durable_mode", None),
            )
        evidence_config_hash = compute_evidence_config_hash(run_dir)

        # 4. Write the evidence.finalized event — this binds the artifacts,
        # task.json, AND the evidence config (policy) to the signed event log.
        # The hashes are stable (don't change when the chain head changes),
        # so they can be verified later even though the manifest is rewritten
        # after this event.
        finalize_payload: dict[str, Any] = {
            "task_id": state["task_id"],
            "run_schema_version": "1.0",
            "artifact_content_hash": artifact_content_hash,
            "artifact_count": artifact_count,
            "event_chain_head_before_finalize": ctx.events.last_hash,
            "final_status": final_status,
        }
        if task_json_hash is not None:
            finalize_payload["task_json_hash"] = task_json_hash
        if evidence_config_hash is not None:
            finalize_payload["evidence_config_hash"] = evidence_config_hash
        ctx.events.write(EventType.EVIDENCE_FINALIZED, finalize_payload)

        # 5. Rewrite the manifest so its chain head includes evidence.finalized.
        _, manifest_hash = write_evidence_manifest(
            run_dir=run_dir,
            events_writer=ctx.events,
        )

        # 6. Re-render the report with the final manifest hash + event timeline.
        # The timeline now includes evidence.finalized.
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
        elif report_path and (review is None or state.get("diff") is None):
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

        # 7. Write the evidence.report_bound event — this binds the report
        # (the human-facing truth surface) to the signed event log. The
        # report_hash is computed AFTER the report is rendered, so it covers
        # the exact bytes the human will read. Tampering with or deleting
        # the report breaks this signed event.
        #
        # Note: we do NOT rewrite the manifest after this event. The run
        # manifest is immutable — it covers the run phase (up to
        # evidence.finalized). evidence.report_bound is a post-run event,
        # verified separately. This avoids a circular dependency between
        # the report's manifest hash and the manifest's chain head.
        report_hash = compute_report_hash(run_dir)
        if report_hash is not None:
            ctx.events.write(
                EventType.EVIDENCE_REPORT_BOUND,
                {
                    "task_id": state["task_id"],
                    "report_hash": report_hash,
                    "manifest_hash": manifest_hash,
                    "event_chain_head_before_report_bound": ctx.events.last_hash,
                },
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
    cfg = state["config"]
    repo_path = state["repo_path"]
    # For docker_sbx with clone mode, the host repo is mounted read-only.
    # The agent works inside the sandbox's private clone, so a dirty host
    # repo is fine — the agent can't modify it.
    is_sbx = cfg.executor.backend == "docker_sbx"
    clean = is_clean(repo_path) if not is_sbx else True
    ctx.events.write(
        EventType.REPO_CHECKED,
        {"repo_path": str(repo_path), "clean": clean, "executor": cfg.executor.backend},
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

    # --- docker_sbx backend: skip worktree, record sandbox metadata ------- #
    if cfg.executor.backend == "docker_sbx":
        try:
            executor = SbxExecutor(cfg.executor)
            executor._validate()  # fail-closed: sbx installed, clone mode, etc.
        except SbxNotInstalledError as exc:
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "create_worktree", "reason": "sbx not installed", "detail": str(exc)},
            )
            task.status = TaskStatus.FAILED
            ctx.store.save(task)
            return {"status": TaskStatus.FAILED, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "create_worktree", "reason": "sbx config invalid", "detail": str(exc)},
            )
            task.status = TaskStatus.FAILED
            ctx.store.save(task)
            return {"status": TaskStatus.FAILED, "error": str(exc)}

        # Record the base commit sha (current HEAD of the base branch).
        from git import Repo
        repo = Repo(str(state["repo_path"]))
        base_sha = repo.heads[cfg.repo.default_branch].commit.hexsha
        task.base_commit_sha = base_sha
        task.status = TaskStatus.WORKTREE_CREATED
        ctx.store.save(task)

        sandbox_name = executor.sandbox_name(state["task_id"])
        sandbox_remote = executor.sandbox_remote(state["task_id"])
        info = executor.sandbox_info(state["task_id"])

        # v0.5.15: Write sandbox.configured (not sandbox.started) here.
        # sandbox.started is written only after the sbx actually launches
        # successfully in run_agent_node. This is intention, not fact.
        ctx.events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": sandbox_name,
                "sandbox_remote": sandbox_remote,
                "executor": info.to_dict(),
            },
        )
        # worktree_path is set to repo_path as a placeholder — the real
        # worktree (from the sandbox remote) is created after the agent runs.
        return {
            "worktree_path": state["repo_path"],
            "sandbox_name": sandbox_name,
            "sandbox_remote": sandbox_remote,
            "sandbox_metadata": info.to_dict(),
        }

    # --- worktree backend (default): current behavior --------------------- #
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

    # --- docker_sbx backend: run agent inside sandbox, then fetch remote -- #
    if cfg.executor.backend == "docker_sbx":
        executor = SbxExecutor(cfg.executor)
        task.status = TaskStatus.EXECUTING
        ctx.store.save(task)
        ctx.events.write(
            EventType.AGENT_STARTED,
            {"agent": f"sbx:{cfg.executor.agent}", "timeout_seconds": cfg.agent.timeout_seconds},
        )

        # v0.5.15: sandbox.started is written ONLY after sbx actually launches.
        # If executor.start() fails, we write sandbox.failed instead.
        try:
            agent_result = executor.start(
                task_id=state["task_id"],
                prompt_path=state["prompt_path"],
                repo_path=state["repo_path"],
                artifact_dir=state["artifacts_dir"],
                timeout_seconds=cfg.agent.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.events.write(
                EventType.SANDBOX_FAILED,
                {
                    "sandbox_name": state.get("sandbox_name", ""),
                    "reason": "sbx run failed to start",
                    "detail": str(exc),
                },
            )
            ctx.events.write(
                EventType.AGENT_FINISHED,
                {"agent": f"sbx:{cfg.executor.agent}", "exit_code": -1, "summary": f"sbx failed: {exc}"},
            )
            task.status = TaskStatus.FAILED
            ctx.store.save(task)
            return {"status": TaskStatus.FAILED, "error": str(exc)}

        # sbx launched successfully — now write sandbox.started (fact, not intention).
        ctx.events.write(
            EventType.SANDBOX_STARTED,
            {
                "sandbox_name": state.get("sandbox_name", ""),
                "sandbox_remote": state.get("sandbox_remote", ""),
            },
        )

        if agent_result is None:
            raise RuntimeError(
                "sbx executor returned None instead of an AgentResult — "
                "this is a bug in the executor implementation"
            )
        ctx.events.write(
            EventType.AGENT_FINISHED,
            {
                "agent": agent_result.agent_name,
                "exit_code": agent_result.exit_code,
                "summary": agent_result.summary,
            },
        )
        # After the agent finishes, fetch the sandbox remote and create a
        # temporary worktree from it so the existing test runner and diff
        # capture operate on the agent's actual changes.
        # v0.5.15: Use cfg.repo.default_branch instead of hardcoded "main".
        try:
            executor.fetch_remote(state["repo_path"])
            sandbox_wt_path = ctx.store.worktree_path(state["task_id"])
            create_worktree_from_ref(
                repo_path=state["repo_path"],
                ref=f"{state['sandbox_remote']}/{cfg.repo.default_branch}",
                target_path=sandbox_wt_path,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "run_agent", "reason": "sandbox remote fetch/worktree failed", "detail": str(exc)},
            )
        return {
            "agent_result": agent_result,
            "worktree_path": sandbox_wt_path if sandbox_wt_path.exists() else state["repo_path"],
        }

    # --- worktree backend (default): current behavior --------------------- #
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
    if agent_result is None:
        raise RuntimeError(
            f"agent '{agent.name}' returned None instead of an AgentResult — "
            f"this is a bug in the agent implementation"
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

    # --- docker_sbx backend: diff from sandbox remote, not worktree ------- #
    if cfg.executor.backend == "docker_sbx":
        sandbox_remote = state.get("sandbox_remote", "")
        if not sandbox_remote:
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "capture_diff", "reason": "no sandbox remote in state"},
            )
            return {"status": TaskStatus.FAILED, "error": "no sandbox remote in state"}
        try:
            diff: DiffCapture = capture_diff_from_remote(
                repo_path=state["repo_path"],
                remote=sandbox_remote,
                base_branch=base_sha or cfg.repo.default_branch,
                artifacts_dir=state["artifacts_dir"],
                remote_branch=cfg.repo.default_branch,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.events.write(
                EventType.NODE_FAILED,
                {"node": "capture_diff", "reason": "sandbox remote diff failed", "detail": str(exc)},
            )
            return {"status": TaskStatus.FAILED, "error": str(exc)}
        ctx.events.write(
            EventType.DIFF_CAPTURED,
            {
                "files": len(diff.changed_files),
                "insertions": diff.insertions,
                "deletions": diff.deletions,
                "binary_files": diff.binary_files,
                "source": "sandbox_remote",
            },
        )
        return {"diff": diff}

    # --- worktree backend (default): current behavior --------------------- #
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
            "binary_files": diff.binary_files,
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
        worktree_path=state.get("worktree_path"),
    )
    # Add binary file warnings as review concerns — binary changes are hard
    # to review and should be flagged for human attention.
    diff = state["diff"]
    if hasattr(diff, "binary_files") and diff.binary_files:
        for bf in diff.binary_files:
            review.concerns.append(f"binary file changed: {bf}")
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
    diff = state.get("diff", DiffCapture(patch="", stat="", changed_files=[], insertions=0, deletions=0, binary_files=[]))

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


def _cleanup_sandbox(state: dict[str, Any], ctx: NodeContext) -> None:
    """Stop or remove the sandbox after a run (docker_sbx backend only).

    Best-effort: writes a ``sandbox.stopped`` event on success, a
    ``node.failed`` event on failure. Never raises.
    """
    cfg = state.get("config")
    if cfg is None or cfg.executor.backend != "docker_sbx":
        return
    sandbox_name = state.get("sandbox_name", "")
    if not sandbox_name:
        return
    try:
        executor = SbxExecutor(cfg.executor)
        executor._sandbox_name = sandbox_name
        executor._sandbox_remote = state.get("sandbox_remote", "")
        executor.cleanup()
        ctx.events.write(
            EventType.SANDBOX_STOPPED,
            {
                "sandbox_name": sandbox_name,
                "removed": cfg.executor.remove_after_run,
            },
        )
    except Exception as exc:  # noqa: BLE001
        ctx.events.write(
            EventType.NODE_FAILED,
            {"node": "sandbox_cleanup", "message": str(exc)},
        )


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
    _cleanup_sandbox(state, ctx)
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
    _cleanup_sandbox(state, ctx)
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
    manifest_hash = _finalize_evidence(state, ctx)
    _cleanup_sandbox(state, ctx)
    return {
        "status": TaskStatus.NEEDS_REVIEW,
        "report_path": state.get("report_path"),
        "vault_note_path": state.get("vault_note_path"),
        "manifest_hash": manifest_hash,
    }


# --------------------------------------------------------------------------- #
# v0.6.0: Autonomous mode nodes.
#
# When review.autonomous_mode is True and the task passes all gates, these
# nodes bypass human approval and optionally merge the task branch into the
# default branch. The auto.approved and auto.merged events are written to
# the same hash-chained event log as all other events.
# --------------------------------------------------------------------------- #


def auto_approve_node(
    state: dict[str, Any], ctx: NodeContext,
) -> dict[str, Any]:
    """Bypass human review by programmatically approving the task.

    Only fires when ``config.review.autonomous_mode`` is True. Writes an
    ``auto.approved`` event with the approver set to ``ACP-Autonomous-Bot``.
    The task status is set to ``APPROVED`` and persisted.

    If autonomous mode is not enabled, this node is a no-op — the graph
    routes to ``done`` instead.
    """
    cfg = state.get("config")
    if cfg is None or not cfg.review.autonomous_mode:
        return {}

    task = state["task"]

    ctx.events.write(
        EventType.AUTO_APPROVED,
        {
            "approver": "ACP-Autonomous-Bot",
            "reason": "All gates passed in autonomous mode",
            "gate_outcome": "passed",
        },
    )

    task.status = TaskStatus.APPROVED
    task.touch()
    ctx.store.save(task)

    return {"status": TaskStatus.APPROVED, "auto_approved": True}


def auto_merge_node(
    state: dict[str, Any], ctx: NodeContext,
) -> dict[str, Any]:
    """Merge the task branch into the default branch.

    Only fires when ``config.review.auto_merge_on_pass`` is True (and
    implicitly ``autonomous_mode`` is True, since the graph only routes
    here after auto_approve). Writes an ``auto.merged`` event with the
    merge commit SHA.

    If the merge fails (conflicts, diverged base), the task is downgraded
    to ``NEEDS_REVIEW`` so a human can resolve it manually. The merge
    failure is recorded in the event log.
    """
    cfg = state.get("config")
    if cfg is None or not cfg.review.auto_merge_on_pass:
        return {}

    task = state["task"]
    repo_path = state.get("repo_path")
    if repo_path is None:
        return {}

    try:
        from acp.gitops.merge import merge_to_base

        merge_sha = merge_to_base(
            repo_path=Path(repo_path),
            task_branch=task.task_branch,
            base_branch=cfg.repo.default_branch,
        )

        ctx.events.write(
            EventType.AUTO_MERGED,
            {
                "task_branch": task.task_branch,
                "base_branch": cfg.repo.default_branch,
                "merge_commit_sha": merge_sha,
            },
        )

        return {"auto_merged": True, "merge_commit_sha": merge_sha}
    except Exception as exc:  # noqa: BLE001
        # Merge failed — downgrade to NEEDS_REVIEW for human resolution.
        ctx.events.write(
            EventType.NODE_FAILED,
            {
                "node": "auto_merge",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        task.status = TaskStatus.NEEDS_REVIEW
        task.touch()
        ctx.store.save(task)
        return {
            "status": TaskStatus.NEEDS_REVIEW,
            "auto_merged": False,
            "error": f"auto_merge: {exc}",
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

    v0.6.0: When ``cfg.agent.dynamic_test_generation`` is True and the
    review result flags TESTS_MISSING (behavior changed but no test files),
    the repair prompt instructs the agent to write new tests instead of
    only fixing failing commands.
    """
    cfg = state["config"]
    task = state["task"]
    attempts = int(state.get("repair_attempts", 0)) + 1
    max_attempts = cfg.agent.max_repair_attempts

    task.status = TaskStatus.REPAIRING
    ctx.store.save(task)

    failures = extract_failures(state.get("command_results", []))

    # v0.6.0: Detect TESTS_MISSING from the review result. If the
    # RiskEngine flagged this and dynamic_test_generation is enabled,
    # the repair prompt instructs the agent to write tests.
    tests_missing = False
    if cfg.agent.dynamic_test_generation:
        review = state.get("review_result")
        if review is not None and hasattr(review, "concerns"):
            tests_missing = any(
                "tests_missing" in c.lower() or "no test files" in c.lower()
                for c in review.concerns
            )

    prompt_path = write_repair_prompt(
        original_request=state["user_request"],
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
        repo_config=cfg,
        failures=failures,
        attempt=attempts,
        max_attempts=max_attempts,
        tests_missing=tests_missing,
    )

    ctx.events.write(
        EventType.REPAIR_ATTEMPTED,
        {
            "attempt": attempts,
            "max_attempts": max_attempts,
            "failures": len(failures),
            "prompt_path": str(prompt_path),
            "tests_missing": tests_missing,
        },
    )

    history = list(state.get("repair_history", []))
    history.append({
        "attempt": attempts,
        "prompt_path": str(prompt_path),
        "tests_missing": tests_missing,
    })

    # v0.6.0: Compute a fingerprint of the current failure signature.
    # The circuit breaker in _route_after_tests uses this to detect when
    # the agent is repeating the same fix. The fingerprint is a hash of
    # the failing command names + exit codes (not stdout/stderr, which
    # may vary slightly even for the same root cause).
    import hashlib
    fp_input = "|".join(
        f"{f['command']}:{f['exit_code']}" for f in failures
    )
    fingerprint = hashlib.sha256(
        fp_input.encode()
    ).hexdigest()[:16] if fp_input else "no_failures"
    fingerprints = list(state.get("repair_fingerprints", []))
    fingerprints.append(fingerprint)

    return {
        "repair_attempts": attempts,
        "repair_history": history,
        "prompt_path": prompt_path,
        "repair_fingerprints": fingerprints,
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
    if agent_result is None:
        raise RuntimeError(
            f"agent '{agent.name}' returned None instead of an AgentResult — "
            f"this is a bug in the agent implementation"
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
