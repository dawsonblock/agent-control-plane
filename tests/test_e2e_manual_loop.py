"""M1 acceptance test — the full manual evidence loop, end to end.

This is the gate for Milestone 1. It exercises the real ``EvidenceLoop``
against a disposable git repo, with the shell agent in its non-interactive
test mode, and asserts every M1 acceptance criterion from the plan:

  - worktree dir exists
  - events.jsonl has ≥ the expected events, in order
  - commands.json has one result per configured command
  - review.json has a risk level
  - final_report.md exists and references the task id
  - vault/tasks/<id>.md exists with approved: false
  - main branch HEAD is unchanged  (the core safety invariant)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acp.cli import EvidenceLoop
from acp.config import CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.errors import RepoDirtyError
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _config_for(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        commands=CommandsSection(
            install="",
            lint='echo "lint ok"',
            typecheck="",
            test='echo "all tests passed"',
            build="",
        ),
        review=ReviewSection(
            max_changed_files=20,
            max_added_lines=1000,
        ),
    )


# The ordered events every successful run must produce, at minimum.
_EXPECTED_EVENT_ORDER = [
    EventType.TASK_CREATED,
    EventType.REPO_CHECKED,
    EventType.WORKTREE_CREATED,
    EventType.CONTEXT_BUILT,
    EventType.AGENT_STARTED,
    EventType.AGENT_FINISHED,
    EventType.DIFF_CAPTURED,
    EventType.REVIEW_COMPLETED,
    EventType.REPORT_WRITTEN,
    EventType.VAULT_NOTE_WRITTEN,
    EventType.TASK_COMPLETED,
]


def test_e2e_manual_loop_passes(disposable_repo, isolated_workspace):
    repo = disposable_repo
    cfg = _config_for(repo.path)
    store = TaskStore(runs_root=isolated_workspace["runs_root"])

    loop = EvidenceLoop(
        config=cfg,
        user_request="Add a hello-world test",
        store=store,
        vault_root=isolated_workspace["vault_root"],
    )
    result = loop.run()

    run_dir = result.run_dir
    artifacts = run_dir / "artifacts"

    # --- worktree exists ------------------------------------------------ #
    assert (run_dir / "worktree").is_dir(), "worktree directory missing"

    # --- events.jsonl: order + presence --------------------------------- #
    events_path = run_dir / "events.jsonl"
    assert events_path.is_file(), "events.jsonl missing"
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    event_types = [EventType(e["type"]) for e in events]
    for expected in _EXPECTED_EVENT_ORDER:
        assert expected in event_types, f"missing event {expected.value}"
    # The ordered subset must appear in order.
    idx = 0
    for expected in _EXPECTED_EVENT_ORDER:
        idx = event_types.index(expected, idx)
    assert len(events) >= len(_EXPECTED_EVENT_ORDER)

    # --- commands.json -------------------------------------------------- #
    commands_path = artifacts / "commands.json"
    assert commands_path.is_file()
    cmds = json.loads(commands_path.read_text())
    # install/typecheck/build are empty → skipped/not-run; lint + test ran.
    ran_names = {c["command"] for c in cmds if not c.get("skipped")}
    assert 'echo "lint ok"' in ran_names
    assert 'echo "all tests passed"' in ran_names
    assert all(c["exit_code"] == 0 for c in cmds), "a command unexpectedly failed"

    # --- review.json ---------------------------------------------------- #
    review_path = artifacts / "review.json"
    assert review_path.is_file()
    review = json.loads(review_path.read_text())
    assert review["risk"] in ("low", "medium", "high")
    assert review["recommendation"] in ("merge", "revise", "reject")

    # --- final_report.md ------------------------------------------------ #
    report_path = artifacts / "final_report.md"
    assert report_path.is_file()
    body = report_path.read_text()
    assert result.task_id in body
    assert review["risk"] in body
    assert "Changed files" in body or "Changed Files" in body.lower() or "file(s) changed" in body.lower()

    # --- diff artifacts ------------------------------------------------- #
    assert (artifacts / "diff.patch").is_file()
    assert (artifacts / "diff_stat.txt").is_file()
    assert (artifacts / "agent_prompt.txt").is_file()

    # --- Obsidian note, shipped as draft, unapproved -------------------- #
    note_path = isolated_workspace["vault_root"] / "tasks" / f"{result.task_id}.md"
    assert note_path.is_file(), "vault note missing"
    note = note_path.read_text()
    assert "approved: false" in note
    assert "memory_status: draft" in note
    assert "graphiti_ingested: false" in note

    # --- status reflects a clean pass ----------------------------------- #
    assert result.status == TaskStatus.PASSED

    # --- CORE SAFETY INVARIANT: main is untouched ----------------------- #
    import subprocess
    main_now = subprocess.run(
        ["git", "-C", str(repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert main_now == repo.main_head, "MAIN BRANCH WAS MODIFIED — critical safety violation"


def test_e2e_dirty_repo_fails_fast(disposable_repo, isolated_workspace):
    """A dirty repo must fail before any worktree is created."""
    repo = disposable_repo
    # make the repo dirty
    (repo.path / "README.md").write_text("# dirty change\n")

    cfg = _config_for(repo.path)
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    loop = EvidenceLoop(
        config=cfg,
        user_request="should not run",
        store=store,
        vault_root=isolated_workspace["vault_root"],
    )

    with pytest.raises(RepoDirtyError) as exc_info:
        loop.run()
    assert exc_info.value.exit_code == 2

    # No worktree directory should exist for the failed task.
    run_subdirs = [p for p in isolated_workspace["runs_root"].iterdir() if p.is_dir()]
    for rd in run_subdirs:
        assert not (rd / "worktree").exists(), "worktree created despite dirty repo"


def test_e2e_failing_test_still_writes_report(disposable_repo, isolated_workspace):
    """A failing configured command must still produce a report + vault note."""
    repo = disposable_repo
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=repo.path, default_branch="main"),
        commands=CommandsSection(
            lint="",
            typecheck="",
            test="exit 1",   # always fails
            build="",
        ),
        review=ReviewSection(),
    )
    store = TaskStore(runs_root=isolated_workspace["runs_root"])
    loop = EvidenceLoop(
        config=cfg,
        user_request="task with failing tests",
        store=store,
        vault_root=isolated_workspace["vault_root"],
    )
    result = loop.run()

    # Failed test → task FAILED, but evidence was still captured.
    assert result.status == TaskStatus.FAILED
    assert (result.run_dir / "artifacts" / "final_report.md").is_file()
    note = (isolated_workspace["vault_root"] / "tasks" / f"{result.task_id}.md")
    assert note.is_file()

    # Safety invariant still holds even on failure.
    import subprocess
    main_now = subprocess.run(
        ["git", "-C", str(repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert main_now == repo.main_head
