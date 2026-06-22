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
from typing import Any

from acp.agents.base import write_prompt
from acp.agents.registry import build_agent
from acp.errors import RepoDirtyError
from acp.events import EventWriter
from acp.gitops.diff import DiffCapture, capture_diff
from acp.gitops.worktrees import create_worktree, is_clean
from acp.models import EventType, TaskStatus
from acp.reports.writer import write_report
from acp.review.diff_reviewer import review_diff
from acp.store import TaskStore
from acp.testing.runner import all_passed, run_commands
from acp.vault.obsidian_writer import write_vault_note


@dataclass
class NodeContext:
    """Shared services threaded through the graph, outside of ACPState."""

    store: TaskStore
    events: EventWriter


# --------------------------------------------------------------------------- #
# Node signature: takes (state, ctx) → patch dict.
# We use functools.partial-style binding in workflow.py to inject ctx.
# --------------------------------------------------------------------------- #


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
        # Mark the task failed + write the terminal event; the graph routes
        # to `failed` based on status. Dirty repo is a pre-worktree failure:
        # there's no diff to report on, so the failed node skips the report.
        task = state["task"]
        task.status = TaskStatus.FAILED
        ctx.store.save(task)
        return {"status": TaskStatus.FAILED, "error": f"repo dirty: {repo_path}"}
    return {}


def create_worktree_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    task = state["task"]
    try:
        worktree_path = create_worktree(
            repo_path=state["repo_path"],
            base_branch=cfg.repo.default_branch,
            branch_name=task.task_branch,
            target_path=ctx.store.worktree_path(state["task_id"]),
        )
    except Exception as exc:  # noqa: BLE001
        ctx.events.write(
            EventType.TASK_FAILED,
            {"reason": "worktree creation failed", "detail": str(exc)},
        )
        task.status = TaskStatus.FAILED
        ctx.store.save(task)
        return {"status": TaskStatus.FAILED, "error": str(exc)}
    task.status = TaskStatus.WORKTREE_CREATED
    ctx.store.save(task)
    ctx.events.write(
        EventType.WORKTREE_CREATED,
        {"branch": task.task_branch, "worktree_path": str(worktree_path)},
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
    agent = build_agent(cfg)
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
    command_results = run_commands(
        repo_config=cfg,
        worktree_path=state["worktree_path"],
        artifact_dir=state["artifacts_dir"],
    )
    for r in command_results:
        ctx.events.write(
            EventType.COMMAND_FINISHED,
            {
                "command": r.command,
                "exit_code": r.exit_code,
                "skipped": r.skipped,
                "duration_seconds": r.duration_seconds,
            },
        )
    return {"command_results": command_results}


def capture_diff_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    cfg = state["config"]
    diff: DiffCapture = capture_diff(
        worktree_path=state["worktree_path"],
        base_branch=cfg.repo.default_branch,
        artifacts_dir=state["artifacts_dir"],
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
    # Compute final status *before* writing so the report reflects the truth.
    tests_pass = all_passed(state.get("command_results", []))
    review = state.get("review_result")
    hard_block = bool(review and review.hard_block)
    passed = tests_pass and not hard_block
    task.status = TaskStatus.PASSED if passed else TaskStatus.FAILED
    ctx.store.save(task)

    report_path = write_report(
        task=task,
        command_results=state.get("command_results", []),
        review=review,
        diff=state["diff"],
        artifact_dir=state["artifacts_dir"],
        agent_result=state.get("agent_result"),
    )
    ctx.events.write(EventType.REPORT_WRITTEN, {"report_path": str(report_path)})
    return {"report_path": report_path, "status": task.status}


def write_vault_note_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    task = state["task"]
    vault_note_path = write_vault_note(
        report_body=state["report_path"].read_text(),
        task=task,
        review=state["review_result"],
        diff=state["diff"],
        vault_root=state["vault_root"],
    )
    ctx.events.write(
        EventType.VAULT_NOTE_WRITTEN,
        {"vault_note_path": str(vault_note_path)},
    )
    return {"vault_note_path": vault_note_path}


def done_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Terminal success node. Writes the task.completed event."""
    task = state["task"]
    final = EventType.TASK_COMPLETED if task.status == TaskStatus.PASSED else EventType.TASK_FAILED
    ctx.events.write(
        final,
        {
            "status": task.status.value,
            "tests_pass": all_passed(state.get("command_results", [])),
            "recommendation": state["review_result"].recommendation.value,
        },
    )
    return {"status": task.status}


def failed_node(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
    """Terminal failure node.

    If we have a diff, we still write a report + vault note — the spec rule:
    "failed task still writes report." If we failed before the worktree (dirty
    repo), there's nothing to report on, so we just finalize the task status.
    """
    task = state["task"]

    # Best-effort evidence write if we got far enough to have a diff.
    if state.get("diff") is not None and state.get("review_result") is not None:
        try:
            report_path = write_report(
                task=task,
                command_results=state.get("command_results", []),
                review=state["review_result"],
                diff=state["diff"],
                artifact_dir=state["artifacts_dir"],
                agent_result=state.get("agent_result"),
            )
            ctx.events.write(EventType.REPORT_WRITTEN, {"report_path": str(report_path)})
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
        except Exception:  # noqa: BLE001
            pass  # evidence is best-effort; don't mask the real failure

    task.status = TaskStatus.FAILED
    ctx.store.save(task)
    ctx.events.write(
        EventType.TASK_FAILED,
        {"status": task.status.value, "error": state.get("error", "unknown")},
    )
    return {"status": TaskStatus.FAILED}
