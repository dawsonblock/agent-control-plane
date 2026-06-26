"""M2 acceptance test — swapping the coder agent is config-only.

The M2 gate (from the plan): "Agent swap requires config change only."
This test runs the *same* EvidenceLoop twice against equivalent setups,
differing only in ``agent.default`` (shell vs custom). It asserts that:
  - both runs produce the full evidence set (report, vault note, events)
  - the workflow code path is identical (same event sequence)
  - only the agent identity in the events differs
  - main is untouched in both cases
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from acp.legacy_loop import EvidenceLoop
from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.errors import AgentConfigError
from acp.models import EventType
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
        commands=CommandsSection(test='echo "tests passed"'),
        review=ReviewSection(),
    )


def _event_types(run_dir: Path) -> list[str]:
    events = (run_dir / "events.jsonl").read_text().splitlines()
    return [json.loads(l)["type"] for l in events if l.strip()]


def test_agent_swap_shell_vs_custom(disposable_repo, isolated_workspace):
    """Same workflow, two agents — evidence structure must match."""
    repo = disposable_repo
    runs = isolated_workspace["runs_root"]
    vault = isolated_workspace["vault_root"]

    # Run 1: manual shell agent (test mode makes a trivial edit)
    loop1 = EvidenceLoop(
        config=_config(repo.path, agent="shell"),
        user_request="demo task",
        store=TaskStore(runs_root=runs / "shell"),
        vault_root=vault / "shell",
    )
    r1 = loop1.run()

    # Run 2: custom CLI agent — a trivial passthrough that echoes the prompt
    # and makes a small edit so a diff exists. This stands in for any real
    # CLI coding agent (Claude Code, Codex, ...); the point is that the
    # workflow treats it identically to the shell agent.
    passthrough = (
        "sh -c \"cat {prompt_path} > {artifact_dir}/agent_stdout.txt && "
        "echo 'agent ran' > {worktree_path}/AGENT_NOTES.md && "
        "mkdir -p {worktree_path}/tests && "
        "echo 'def test_agent(): assert True' > {worktree_path}/tests/test_agent.py\""
    )
    loop2 = EvidenceLoop(
        config=_config(repo.path, agent="custom", command_template=passthrough),
        user_request="demo task",
        store=TaskStore(runs_root=runs / "custom"),
        vault_root=vault / "custom",
    )
    r2 = loop2.run()

    # --- both produced the same evidence structure ---------------------- #
    for r in (r1, r2):
        assert (r.run_dir / "artifacts" / "final_report.md").is_file()
        assert (r.run_dir / "artifacts" / "review.json").is_file()
        assert (r.run_dir / "artifacts" / "commands.json").is_file()
        assert (r.run_dir / "artifacts" / "diff.patch").is_file()
        assert (r.run_dir / "events.jsonl").is_file()

    # --- identical event sequence (the workflow code path) -------------- #
    assert _event_types(r1.run_dir) == _event_types(r2.run_dir), (
        "event sequence differs — agent swap changed the workflow, not just the agent"
    )

    # --- only the agent identity in events differs ---------------------- #
    def _agent_finished_payload(run_dir: Path) -> dict:
        events = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines() if l.strip()]
        return next(e["payload"] for e in events if e["type"] == EventType.AGENT_FINISHED.value)

    p1 = _agent_finished_payload(r1.run_dir)
    p2 = _agent_finished_payload(r2.run_dir)
    assert p1["agent"] == "shell"
    assert p2["agent"] == "custom"
    # Same key set — the result shape is identical regardless of agent.
    assert set(p1) == set(p2)

    # --- main untouched in both cases ----------------------------------- #
    main_now = subprocess.run(
        ["git", "-C", str(repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert main_now == repo.main_head


def test_custom_agent_requires_template(disposable_repo, isolated_workspace):
    """custom with an empty command_template is a config error, not a crash."""
    repo = disposable_repo
    cfg = _config(repo.path, agent="custom", command_template="")
    loop = EvidenceLoop(
        config=cfg,
        user_request="should fail",
        store=TaskStore(runs_root=isolated_workspace["runs_root"]),
        vault_root=isolated_workspace["vault_root"],
    )
    # The loop creates the worktree first, then builds the agent, so the
    # AgentConfigError surfaces at agent build time.
    import pytest
    with pytest.raises(AgentConfigError):
        loop.run()


def test_registry_known_agents():
    """The registry is the single dispatch point and knows both kinds."""
    from acp.agents.registry import known_agents
    assert set(known_agents()) >= {"shell", "custom"}
