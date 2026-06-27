"""v0.5.4 tests — explicit validation_status replaces ambiguous tests_pass.

Covers:
  - ``validation_ran`` / ``validation_passed`` / ``validation_status`` helpers
  - reviewer summary wording: "no validation commands ran" vs "validation passed"
    vs "validation failed" (never "tests pass" for the no-validation case)
  - repair routing triggers only on actual failed non-skipped commands, not on
    the no-validation case that the old empty-list-as-pass pattern masked
"""

from __future__ import annotations

import json
from pathlib import Path

from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter
from acp.gitops.diff import DiffCapture
from acp.models import CommandResult, EventType, TaskStatus
from acp.review.diff_reviewer import review_diff
from acp.store import TaskStore
from acp.testing.runner import (
    validation_passed,
    validation_ran,
    validation_status,
)

# --------------------------------------------------------------------------- #
# CommandResult builder
# --------------------------------------------------------------------------- #


def _cmd(exit_code: int = 0, skipped: bool = False) -> CommandResult:
    return CommandResult(
        command="echo test" if not skipped else "",
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout_path=Path("/tmp/stdout"),
        stderr_path=Path("/tmp/stderr"),
        duration_seconds=0.1,
        skipped=skipped,
    )


def _diff() -> DiffCapture:
    return DiffCapture(
        patch="diff --git a/src/main.py b/src/main.py\n+def foo():\n+    return 1\n",
        stat="1 file changed, 2 insertions(+)",
        changed_files=["src/main.py"],
        insertions=2,
        deletions=0,
    )


def _cfg() -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=Path("/tmp/demo"), default_branch="main"),
        agent=AgentSection(),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
    )


# --------------------------------------------------------------------------- #
# Helper unit tests
# --------------------------------------------------------------------------- #


def test_validation_ran_false_when_all_skipped() -> None:
    assert validation_ran([_cmd(0, skipped=True), _cmd(0, skipped=True)]) is False


def test_validation_ran_false_for_empty_list() -> None:
    assert validation_ran([]) is False


def test_validation_ran_true_when_any_non_skipped() -> None:
    assert validation_ran([_cmd(0, skipped=True), _cmd(0)]) is True


def test_validation_passed_false_when_no_validation_ran() -> None:
    # The key fix: "skipped" is not a flavor of "passed".
    assert validation_passed([]) is False
    assert validation_passed([_cmd(0, skipped=True)]) is False


def test_validation_passed_true_when_ran_and_all_pass() -> None:
    assert validation_passed([_cmd(0), _cmd(0)]) is True


def test_validation_passed_false_when_any_failed() -> None:
    assert validation_passed([_cmd(0), _cmd(1)]) is False


def test_validation_status_three_states() -> None:
    assert validation_status([]) == "skipped"
    assert validation_status([_cmd(0, skipped=True)]) == "skipped"
    assert validation_status([_cmd(0), _cmd(0)]) == "passed"
    assert validation_status([_cmd(0), _cmd(1)]) == "failed"
    assert validation_status([_cmd(0, skipped=True), _cmd(1)]) == "failed"


# --------------------------------------------------------------------------- #
# Reviewer summary wording
# --------------------------------------------------------------------------- #


def _review(command_results, tmp_path: Path) -> str:
    review = review_diff(
        diff=_diff(),
        command_results=command_results,
        repo_config=_cfg(),
        artifacts_dir=tmp_path,
    )
    return review.summary


def test_summary_no_validation_says_no_commands_ran(tmp_path: Path) -> None:
    summary = _review([_cmd(0, skipped=True)], tmp_path)
    assert "no validation commands ran" in summary
    assert "tests pass" not in summary
    assert "validation passed" not in summary


def test_summary_no_validation_for_empty_command_list(tmp_path: Path) -> None:
    summary = _review([], tmp_path)
    assert "no validation commands ran" in summary
    assert "tests pass" not in summary


def test_summary_validation_passed(tmp_path: Path) -> None:
    summary = _review([_cmd(0), _cmd(0)], tmp_path)
    assert "validation passed" in summary
    assert "tests pass" not in summary
    assert "no validation commands ran" not in summary


def test_summary_validation_failed(tmp_path: Path) -> None:
    summary = _review([_cmd(0), _cmd(1)], tmp_path)
    assert "validation failed" in summary
    assert "tests failing" not in summary
    assert "no validation commands ran" not in summary


# --------------------------------------------------------------------------- #
# Repair routing — no validation must not trigger repair
# --------------------------------------------------------------------------- #


async def _run_graph(repo_path, runs_root, vault_root, *, test_cmd="", max_repair_attempts=2):
    store = TaskStore(runs_root=runs_root)
    events = EventWriter("__pending__", store.root / "__pending__")
    from acp.graph.workflow import build_workflow

    wf = build_workflow(store=store, events=events)
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=max_repair_attempts),
        commands=CommandsSection(test=test_cmd),  # empty → skipped
        review=ReviewSection(),
    )
    from acp.graph.state import initial_state

    state = initial_state(
        config=cfg,
        user_request="no-validation task",
        vault_root=vault_root,
        runs_root=runs_root,
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "no-val"}})
    return result, store


def _event_types(store, task_id):
    p = store.events_path(task_id)
    return [json.loads(l)["type"] for l in p.read_text().splitlines() if l.strip()]


async def test_no_validation_does_not_trigger_repair(disposable_repo, isolated_workspace):
    """All commands skipped + max_repair_attempts=2 → no repair attempts.

    Previously the empty-list-as-pass pattern returned True so repair was
    skipped, but for the wrong reason. Now routing checks for actual failures
    explicitly, so
    no-validation flows straight to capture_diff → review → NEEDS_REVIEW.
    """
    result, store = await _run_graph(
        disposable_repo.path,
        isolated_workspace["runs_root"],
        isolated_workspace["vault_root"],
        test_cmd="",  # skipped → no validation
        max_repair_attempts=2,
    )

    # No validation ran → NEEDS_REVIEW (not FAILED, not PASSED).
    assert result["status"] == TaskStatus.NEEDS_REVIEW, (
        f"expected NEEDS_REVIEW for no-validation, got {result['status']}"
    )

    events = _event_types(store, result["task_id"])
    repair_events = [
        e
        for e in events
        if e in (EventType.REPAIR_ATTEMPTED.value, EventType.REPAIR_EXHAUSTED.value)
    ]
    assert repair_events == [], f"no validation ran — repair must not trigger, got {repair_events}"
