"""M2 acceptance test — swapping the coder agent is config-only.

The M2 gate (from the plan): "Agent swap requires config change only."
This test runs the *same* LangGraph workflow twice against equivalent
setups, differing only in ``agent.default`` (shell vs custom). It asserts
that:
  - both runs produce the full evidence set (report, vault note, events)
  - the workflow code path is identical (same event sequence)
  - only the agent identity in the events differs
  - main is untouched in both cases

v0.7.6: Migrated from the legacy ``EvidenceLoop`` to the LangGraph workflow
when the linear loop was eradicated. The graph is now the sole engine.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from acp.config import (
    AgentSection,
    CommandsSection,
    ExecutorSection,
    RepoConfig,
    RepoSection,
    ReviewSection,
)
from acp.events import EventWriter
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _config(repo_path: Path, *, agent: str, command_template: str = "") -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(
            default=agent,
            command_template=command_template,
            timeout_seconds=60,
            allow_shell=True,  # test uses shell features (pipes, redirects)
        ),
        # Tests use a trusted, hardcoded agent command — opt into worktree+shell
        # explicitly. The graph's run_agent_node refuses worktree+shell without
        # this flag (RCE protection).
        executor=ExecutorSection(backend="worktree", danger_allow_host_shell=True),
        commands=CommandsSection(test='echo "tests passed"'),
        review=ReviewSection(),
    )


async def _run_graph(
    repo_path: Path,
    runs_root: Path,
    vault_root: Path,
    *,
    agent: str,
    command_template: str = "",
):
    """Build + invoke the graph with the given agent config, returning (final_state, store)."""
    store = TaskStore(runs_root=runs_root)
    # Placeholder writer; create_task node relocates it.
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)

    cfg = _config(repo_path, agent=agent, command_template=command_template)
    state = initial_state(
        config=cfg,
        user_request="demo task",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "acp-test"}})
    return result, store


def _event_types(store, task_id: str) -> list[str]:
    p = store.events_path(task_id)
    if not p.exists():
        return []
    return [json.loads(l)["type"] for l in p.read_text().splitlines() if l.strip()]


async def test_agent_swap_shell_vs_custom(disposable_repo, isolated_workspace):
    """Same workflow, two agents — evidence structure must match."""
    repo = disposable_repo
    runs = isolated_workspace["runs_root"]
    vault = isolated_workspace["vault_root"]

    # Run 1: manual shell agent (test mode makes a trivial edit)
    r1, s1 = await _run_graph(repo.path, runs / "shell", vault / "shell", agent="shell")

    # Run 2: custom CLI agent — a trivial passthrough that echoes the prompt
    # and makes a small edit so a diff exists. This stands in for any real
    # CLI coding agent (Claude Code, Codex, ...); the point is that the
    # workflow treats it identically to the shell agent.
    passthrough = (
        'sh -c "cat {prompt_path} > {artifact_dir}/agent_stdout.txt && '
        "echo 'agent ran' > {worktree_path}/AGENT_NOTES.md && "
        "mkdir -p {worktree_path}/tests && "
        "echo 'def test_agent(): assert True' > {worktree_path}/tests/test_agent.py\""
    )
    r2, s2 = await _run_graph(
        repo.path, runs / "custom", vault / "custom", agent="custom", command_template=passthrough
    )

    # --- both produced the same evidence structure ---------------------- #
    for r, s in ((r1, s1), (r2, s2)):
        task_id = r["task_id"]
        assert (s.artifacts_dir(task_id) / "final_report.md").is_file()
        assert (s.artifacts_dir(task_id) / "review.json").is_file()
        assert (s.artifacts_dir(task_id) / "commands.json").is_file()
        assert (s.artifacts_dir(task_id) / "diff.patch").is_file()
        assert s.events_path(task_id).is_file()

    # --- identical event sequence (the workflow code path) -------------- #
    e1 = _event_types(s1, r1["task_id"])
    e2 = _event_types(s2, r2["task_id"])
    assert e1 == e2, "event sequence differs — agent swap changed the workflow, not just the agent"

    # --- only the agent identity in events differs ---------------------- #
    def _agent_finished_payload(store, task_id: str) -> dict:
        events = [
            json.loads(l) for l in store.events_path(task_id).read_text().splitlines() if l.strip()
        ]
        return next(e["payload"] for e in events if e["type"] == EventType.AGENT_FINISHED.value)

    p1 = _agent_finished_payload(s1, r1["task_id"])
    p2 = _agent_finished_payload(s2, r2["task_id"])
    assert p1["agent"] == "shell"
    assert p2["agent"] == "custom"
    # Same key set — the result shape is identical regardless of agent.
    assert set(p1) == set(p2)

    # --- main untouched in both cases ----------------------------------- #
    main_now = subprocess.run(
        ["git", "-C", str(repo.path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert main_now == repo.main_head


async def test_custom_agent_requires_template(disposable_repo, isolated_workspace):
    """custom with an empty command_template is a config error, not a crash.

    The graph's node error handler catches the AgentConfigError raised when
    build_agent validates the empty command_template, routes to the failed
    terminal node, and the run ends with status=FAILED.
    """
    repo = disposable_repo
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)

    cfg = _config(repo.path, agent="custom", command_template="")
    state = initial_state(
        config=cfg,
        user_request="should fail",
        vault_root=isolated_workspace["vault_root"],
        runs_root=isolated_workspace["runs_root"],
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "acp-test"}})

    # The config error surfaces at agent build time (run_agent node) and is
    # caught by the node error handler → FAILED terminal status.
    assert result["status"] == TaskStatus.FAILED
    task_id = result["task_id"]
    events_list = [
        json.loads(l) for l in store.events_path(task_id).read_text().splitlines() if l.strip()
    ]
    # A node_failed event records the AgentConfigError.
    node_failures = [e for e in events_list if e["type"] == "node.failed"]
    assert node_failures, "expected a node.failed event for the AgentConfigError"
    assert "AgentConfigError" in node_failures[0]["payload"].get("exception_type", "")


def test_registry_known_agents():
    """The registry is the single dispatch point and knows both kinds."""
    from acp.agents.registry import known_agents

    assert set(known_agents()) >= {"shell", "custom"}
