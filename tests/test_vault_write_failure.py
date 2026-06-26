"""v0.5.4 test — vault-note write failure must not leave a PASSED report.

When ``write_report_node`` would mark a task PASSED but the vault-note write
then fails, the task is downgraded to NEEDS_REVIEW. The on-disk
``final_report.md`` and the ``GateResult`` must be re-rendered to match the
downgraded status — otherwise the report says "passed" while the terminal
status is "needs_review" (an evidence/status mismatch).
"""

from __future__ import annotations

import json
from pathlib import Path


from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.review.gates import GateOutcome
from acp.store import TaskStore


def _config(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(lint='echo "lint ok"', test='echo "tests passed"'),
        review=ReviewSection(),
    )


def _run_graph(repo_path, runs_root, vault_root, *, agent_factory=None):
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events, agent_factory=agent_factory)
    cfg = _config(repo_path)
    state = initial_state(
        config=cfg,
        user_request="would-pass task",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    return wf.invoke(state, config={"configurable": {"thread_id": "vault-fail"}}), store


def _events(store, task_id):
    p = store.events_path(task_id)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_vault_write_failure_downgrades_passed_to_needs_review_and_rerenders_report(
    disposable_repo, isolated_workspace, monkeypatch
):
    """Vault write fails on a would-be PASSED task → report + status agree as NEEDS_REVIEW."""
    # Force the vault writer to fail inside the nodes module.
    import acp.graph.nodes as nodes

    def _boom(**_kwargs):
        raise RuntimeError("simulated vault write failure")

    monkeypatch.setattr(nodes, "write_vault_note", _boom)

    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
    )

    # The task was downgraded from PASSED → NEEDS_REVIEW because the vault
    # (review surface) was not written.
    assert result["status"] == TaskStatus.NEEDS_REVIEW, (
        f"expected NEEDS_REVIEW after vault failure, got {result['status']}"
    )

    # The GateResult was downgraded too, so the report's Gate Summary agrees.
    gate_result = result.get("gate_result")
    assert gate_result is not None
    assert gate_result.outcome == GateOutcome.NEEDS_REVIEW
    assert any("Vault note write failed" in r for r in gate_result.reasons)

    # The on-disk report must reflect the downgraded status, not PASSED.
    report_path = Path(str(result["report_path"]))
    assert report_path.is_file(), "report should exist (re-rendered after downgrade)"
    body = report_path.read_text()
    assert "needs review" in body.lower(), (
        f"report should say 'needs review' after downgrade; got:\n{body[:500]}"
    )
    # The "Final gate outcome" line must not claim passed.
    assert "passed" not in _final_outcome_line(body).lower()

    # A node.failed event was written for the vault failure.
    events = _events(store, result["task_id"])
    node_failed = [e for e in events if e["type"] == EventType.NODE_FAILED.value]
    assert any("vault" in e["payload"].get("node", "") for e in node_failed), (
        f"expected a node.failed event for the vault write, got {node_failed}"
    )

    # The terminal task event is needs_review (not completed/passed).
    # evidence.finalized + evidence.report_bound are written after the terminal
    # event to bind artifacts + report to the signed event log — but the task
    # status event is what matters for the status check.
    task_events = [e for e in events if e["type"].startswith("task.")]
    terminal = task_events[-1]
    assert terminal["type"] == EventType.TASK_NEEDS_REVIEW.value
    assert terminal["payload"]["status"] == TaskStatus.NEEDS_REVIEW.value

    # No vault note path was recorded (the write raised).
    assert result.get("vault_note_path") is None


def _final_outcome_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip().startswith("**Final gate outcome:**"):
            return line
    return ""
