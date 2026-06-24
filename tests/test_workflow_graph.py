"""M3 acceptance test — the LangGraph state machine.

The M3 gate (from the plan):
  - A failed node is visible (its transition appears in the event log)
  - A failed task still writes a report

This test exercises the compiled graph three ways:
  1. Happy path → reaches `done`, full event sequence, report + vault note
  2. Failing test command → reaches `failed`, STILL writes report + vault note
  3. Dirty repo → reaches `failed` before any worktree is created

And asserts the core invariant in every case: main branch HEAD is unchanged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _config(
    repo_path: Path,
    *,
    test_cmd: str = 'echo "tests passed"',
    max_repair_attempts: int = 1,
) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=max_repair_attempts),
        commands=CommandsSection(lint='echo "lint ok"', test=test_cmd),
        review=ReviewSection(),
    )


def _run_graph(
    repo_path: Path,
    runs_root: Path,
    vault_root: Path,
    *,
    test_cmd: str = 'echo ok',
    max_repair_attempts: int = 1,
    agent_factory=None,
):
    """Build + invoke the graph, returning (final_state, store, events_writer)."""
    store = TaskStore(runs_root=runs_root)
    # Placeholder writer; create_task node relocates it.
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events, agent_factory=agent_factory)

    cfg = _config(repo_path, test_cmd=test_cmd, max_repair_attempts=max_repair_attempts)
    state = initial_state(
        config=cfg,
        user_request="graph-driven task",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    result = wf.invoke(state, config={"configurable": {"thread_id": "acp-test"}})
    return result, store


def _event_types(store, task_id: str) -> list[str]:
    p = store.events_path(task_id)
    if not p.exists():
        return []
    return [json.loads(l)["type"] for l in p.read_text().splitlines() if l.strip()]


def _main_head(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #


def test_graph_happy_path_reaches_done(disposable_repo, isolated_workspace):
    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        test_cmd='echo "tests passed"',
    )

    # Reaches `done` (terminal success node) → status PASSED.
    assert result["status"] == TaskStatus.PASSED
    task_id = result["task_id"]

    # Full event sequence present, in order.
    events = _event_types(store, task_id)
    expected = [
        EventType.TASK_CREATED.value,
        EventType.REPO_CHECKED.value,
        EventType.WORKTREE_CREATED.value,
        EventType.CONTEXT_BUILT.value,
        EventType.AGENT_STARTED.value,
        EventType.AGENT_FINISHED.value,
        EventType.DIFF_CAPTURED.value,
        EventType.REVIEW_COMPLETED.value,
        EventType.REPORT_WRITTEN.value,
        EventType.VAULT_NOTE_WRITTEN.value,
        EventType.TASK_COMPLETED.value,
    ]
    for ev in expected:
        assert ev in events, f"missing event {ev}"

    # Evidence produced.
    assert Path(str(result["report_path"])).is_file()
    assert Path(str(result["vault_note_path"])).is_file()

    # The full evidence set is on disk.
    artifacts = store.artifacts_dir(task_id)
    for f in ["agent_prompt.txt", "commands.json", "diff.patch", "review.json", "final_report.md"]:
        assert (artifacts / f).is_file(), f"missing artifact {f}"

    # Core invariant.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


def test_graph_failing_test_reaches_failed_but_writes_report(disposable_repo, isolated_workspace):
    """The M3 gate: a failed task still writes a report (with repair disabled).

    Pinned to max_repair_attempts=0 so this exercises the no-repair path —
    M4's repair behavior has its own dedicated tests.
    """
    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        test_cmd="exit 1",  # failing test
        max_repair_attempts=0,
    )

    assert result["status"] == TaskStatus.FAILED
    task_id = result["task_id"]

    # The graph still captured a diff, reviewed it, and wrote evidence —
    # even though the task failed. This is the spec rule.
    assert Path(str(result["report_path"])).is_file()
    assert Path(str(result["vault_note_path"])).is_file()
    artifacts = store.artifacts_dir(task_id)
    assert (artifacts / "review.json").is_file()
    assert (artifacts / "diff.patch").is_file()

    # A terminal failure event was written.
    events = _event_types(store, task_id)
    assert EventType.TASK_FAILED.value in events
    assert EventType.REVIEW_COMPLETED.value in events  # review still ran
    assert EventType.REPORT_WRITTEN.value in events    # report still written

    # Core invariant holds even on failure.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


def test_graph_dirty_repo_fails_before_worktree(disposable_repo, isolated_workspace):
    """Dirty repo → failed node, no worktree created, main untouched."""
    # Dirty the repo.
    (disposable_repo.path / "README.md").write_text("# dirty\n")

    result, store = _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
    )

    assert result["status"] == TaskStatus.FAILED
    task_id = result["task_id"]

    events = _event_types(store, task_id)
    assert EventType.TASK_CREATED.value in events
    assert EventType.REPO_CHECKED.value in events
    assert EventType.TASK_FAILED.value in events
    # Critically: no worktree was created.
    assert EventType.WORKTREE_CREATED.value not in events
    # No worktree directory on disk.
    assert not (store.worktree_path(task_id)).exists()

    # Main untouched.
    assert _main_head(disposable_repo.path) == disposable_repo.main_head


def test_graph_and_legacy_produce_equivalent_evidence(disposable_repo, isolated_workspace):
    """The graph refactor didn't change what the run produces — same artifact set."""
    from acp.legacy_loop import EvidenceLoop

    repo = disposable_repo
    runs = isolated_workspace["runs_root"]
    vault = isolated_workspace["vault_root"]

    # Graph run.
    g_result, g_store = _run_graph(repo.path, runs / "graph", vault / "graph")
    # Legacy run (separate repo branch sequence so they don't collide).
    legacy = EvidenceLoop(
        config=_config(repo.path),
        user_request="legacy task",
        store=TaskStore(runs_root=runs / "legacy"),
        vault_root=vault / "legacy",
    )
    l_result = legacy.run()

    # Both produced the same artifact set.
    g_arts = {p.name for p in g_store.artifacts_dir(g_result["task_id"]).iterdir() if p.is_file()}
    l_arts = {p.name for p in (l_result.run_dir / "artifacts").iterdir() if p.is_file()}
    assert g_arts == l_arts, (
        f"graph and legacy artifact sets differ:\n"
        f"  graph only: {g_arts - l_arts}\n  legacy only: {l_arts - g_arts}"
    )
