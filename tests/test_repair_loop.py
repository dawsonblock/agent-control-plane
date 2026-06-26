"""M4 acceptance tests — the repair loop.

The M4 gate (from the plan):
  - A failed test triggers one repair attempt
  - No infinite loops; max attempts enforced
  - A failed repair still writes a final report

These exercise the conditional edge added in M4: after run_tests, failing
tests route to repair_plan when attempts remain, then back to run_tests. The
cap (config.agent.max_repair_attempts) guarantees termination.

The tests use a ``ScriptedAgent`` whose behavior changes per invocation, so
we can make the first agent run produce a failing test and the repair run
fix it (or not) — deterministically, without a real LLM in the loop.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


from acp.agents.base import AgentResult
from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


# --------------------------------------------------------------------------- #
# A controllable agent that runs a script of actions, one per invocation.
# --------------------------------------------------------------------------- #

class ScriptedAgent:
    """Agent whose behavior is a list of callables, consumed one per ``run``.

    Each action receives (prompt_path, worktree_path) and performs whatever
    side effect is needed (e.g. write a file). This lets a test make the
    initial agent produce a failing test and the repair agent fix it.
    """

    name = "scripted"

    def __init__(self, actions: list):
        self._actions = list(actions)
        self._calls = 0

    def run(self, *, prompt_path, worktree_path, artifact_dir, timeout_seconds) -> AgentResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "agent_stdout.txt").write_text(f"scripted call {self._calls}\n")
        (artifact_dir / "agent_stderr.txt").write_text("")
        if self._calls < len(self._actions):
            self._actions[self._calls](prompt_path, worktree_path)
        self._calls += 1
        return AgentResult(
            agent_name=self.name,
            exit_code=0,
            stdout_path=artifact_dir / "agent_stdout.txt",
            stderr_path=artifact_dir / "agent_stderr.txt",
            summary=f"scripted agent, call {self._calls}",
        )


def _factory_for(agent: ScriptedAgent):
    """Return a config→agent factory that always returns the same agent."""
    return lambda _cfg: agent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _run(repo_path, runs_root, vault_root, *, agent, test_cmd, max_repair_attempts):
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events, agent_factory=_factory_for(agent))
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=max_repair_attempts),
        commands=CommandsSection(test=test_cmd),
        review=ReviewSection(),
    )
    state = initial_state(
        config=cfg,
        user_request="repair-loop task",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    result = wf.invoke(state, config={"configurable": {"thread_id": "m4-test"}})
    return result, store


def _event_types(store, task_id):
    p = store.events_path(task_id)
    return [json.loads(l)["type"] for l in p.read_text().splitlines() if l.strip()]


def _main_head(repo_path):
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# Test 1: repair succeeds — failing test is fixed on the repair attempt.
# --------------------------------------------------------------------------- #

def test_repair_loop_fixes_failing_test(disposable_repo, isolated_workspace):
    """A failing test triggers one repair attempt that fixes it → PASSED.

    The test command reads a marker file; the initial agent writes a marker
    that makes the test fail, and the repair agent overwrites it so the test
    passes. This is a genuine repair, not a faked exit code.
    """
    repo = disposable_repo
    marker = "marker.txt"
    # The test command fails if the marker contains "bad", passes otherwise.
    test_cmd = f"! grep -q bad {marker}"

    def initial_action(prompt, worktree):
        (worktree / marker).write_text("bad\n")

    def repair_action(prompt, worktree):
        (worktree / marker).write_text("good\n")

    agent = ScriptedAgent([initial_action, repair_action])

    result, store = _run(
        repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        agent=agent,
        test_cmd=test_cmd,
        max_repair_attempts=1,
    )

    # Repair succeeded — tests pass, but the diff may get NEEDS_REVIEW
    # (tests pass, reviewer flags concerns). The important thing is the
    # repair loop worked: it ran, fixed the test, produced evidence.
    assert result["status"] in (TaskStatus.PASSED, TaskStatus.NEEDS_REVIEW), (
        f"repair should fix the test, got {result['status']}"
    )

    events = _event_types(store, result["task_id"])
    # One repair attempt → one repair.attempted event. No repair.exhausted
    # because the repair succeeded (tests pass, no further repair needed).
    repair_rounds = sum(
        1 for e in events
        if e in (EventType.REPAIR_ATTEMPTED.value, EventType.REPAIR_EXHAUSTED.value)
    )
    assert repair_rounds == 1, f"expected 1 repair event, got {repair_rounds}"
    assert EventType.REPAIR_ATTEMPTED.value in events
    # No exhausted event because the repair fixed the test.
    assert EventType.REPAIR_EXHAUSTED.value not in events
    # The final outcome: NEEDS_REVIEW or PASSED both complete.
    if result["status"] == TaskStatus.NEEDS_REVIEW:
        assert EventType.TASK_NEEDS_REVIEW.value in events
    else:
        assert EventType.TASK_COMPLETED.value in events

    # A repair prompt artifact was written.
    artifacts = store.artifacts_dir(result["task_id"])
    assert (artifacts / "repair_prompt_1.txt").is_file()

    # Main untouched.
    assert _main_head(repo.path) == repo.main_head


# --------------------------------------------------------------------------- #
# Test 2: repair exhausted — keeps failing, hits the cap, no infinite loop.
# --------------------------------------------------------------------------- #

def test_repair_loop_caps_at_max_attempts(disposable_repo, isolated_workspace):
    """Failing tests after max attempts fall through to FAILED + report.

    max_repair_attempts=2 → exactly 2 repair attempts, then the run stops
    (no third attempt, no infinite loop) and writes a report.
    """
    repo = disposable_repo
    marker = "marker.txt"
    test_cmd = f"! grep -q bad {marker}"

    def always_bad(prompt, worktree):
        # Every attempt re-writes the bad marker → test always fails.
        (worktree / marker).write_text("bad\n")

    # Initial + 2 repair attempts all leave it bad.
    agent = ScriptedAgent([always_bad, always_bad, always_bad])

    result, store = _run(
        repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        agent=agent,
        test_cmd=test_cmd,
        max_repair_attempts=2,
    )

    # Exhausted → FAILED, but a report was still written.
    assert result["status"] == TaskStatus.FAILED
    assert Path(str(result["report_path"])).is_file()
    assert Path(str(result["vault_note_path"])).is_file()

    events = _event_types(store, result["task_id"])
    # Every repair attempt is logged as repair.attempted (2 attempts = 2 events).
    # repair.exhausted is written once, after the cap is reached and tests
    # still fail — meaning "another repair was needed but the cap blocked it."
    repair_events = [e for e in events if e == EventType.REPAIR_ATTEMPTED.value]
    exhausted = [e for e in events if e == EventType.REPAIR_EXHAUSTED.value]
    assert len(repair_events) == 2, (
        f"expected exactly 2 repair.attempted events, got {len(repair_events)}"
    )
    assert len(exhausted) == 1, (
        f"expected exactly 1 repair.exhausted event, got {len(exhausted)}"
    )
    # A terminal failure event was written and a report produced.
    assert EventType.TASK_FAILED.value in events
    assert EventType.REPORT_WRITTEN.value in events

    # The report mentions the repair attempts.
    body = Path(str(result["report_path"])).read_text()
    assert "Repair attempt" in body or "Repair attempts:" in body

    # Main untouched.
    assert _main_head(repo.path) == repo.main_head


# --------------------------------------------------------------------------- #
# Test 3: max_repair_attempts=0 means no repair attempted at all.
# --------------------------------------------------------------------------- #

def test_repair_disabled_when_max_zero(disposable_repo, isolated_workspace):
    """max_repair_attempts=0 → failing test goes straight to FAILED, no repair."""
    repo = disposable_repo

    def initial_action(prompt, worktree):
        (worktree / "marker.txt").write_text("bad\n")

    agent = ScriptedAgent([initial_action])

    result, store = _run(
        repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        agent=agent,
        test_cmd="exit 1",
        max_repair_attempts=0,
    )

    assert result["status"] == TaskStatus.FAILED
    events = _event_types(store, result["task_id"])
    assert EventType.REPAIR_ATTEMPTED.value not in events
    assert EventType.REPAIR_EXHAUSTED.value not in events
    # Only the initial agent run happened.
    agent_starts = [e for e in events if e == EventType.AGENT_STARTED.value]
    assert len(agent_starts) == 1
