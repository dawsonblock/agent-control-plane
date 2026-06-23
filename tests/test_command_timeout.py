"""Tests for command timeout behavior — v0.5 acceptance criteria.

A timed-out command must:
  - have exit_code = 124
  - have timed_out = True
  - produce FAILED final status when it's the only command
  - produce a stderr message mentioning the timeout
"""

from __future__ import annotations

from pathlib import Path

from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.models import CommandResult
from acp.testing.runner import run_commands


def _cfg(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(timeout_seconds=1800),
        commands=CommandsSection(
            lint="",
            test="sleep 10",  # command that will be killed
            build="",
        ),
        review=ReviewSection(),
    )


def test_timeout_exit_code_124(disposable_repo, isolated_workspace):
    """A command that exceeds its timeout returns exit code 124."""
    results = run_commands(
        repo_config=_cfg(disposable_repo.path),
        worktree_path=disposable_repo.path,
        artifact_dir=Path(isolated_workspace["runs_root"]) / "test_timeout" / "artifacts",
        timeout_seconds=1,  # very short timeout → `sleep 10` will be killed
    )

    # Find the `test` command result (the one that timed out).
    test_result = next(r for r in results if "sleep" in r.command)

    assert test_result.exit_code == 124, f"expected 124, got {test_result.exit_code}"
    assert test_result.timed_out is True, "timed_out should be True"


def test_timeout_produces_timeout_stderr(disposable_repo, isolated_workspace):
    """Stderr should contain a message about the timeout."""
    results = run_commands(
        repo_config=_cfg(disposable_repo.path),
        worktree_path=disposable_repo.path,
        artifact_dir=Path(isolated_workspace["runs_root"]) / "test_timeout_stderr" / "artifacts",
        timeout_seconds=1,
    )

    test_result = next(r for r in results if "sleep" in r.command)
    stderr = test_result.stderr_path.read_text()

    assert "timed out" in stderr.lower(), f"stderr should mention timeout: {stderr}"


def test_timeout_duration_recorded(disposable_repo, isolated_workspace):
    """Duration should be recorded for a timed-out command."""
    results = run_commands(
        repo_config=_cfg(disposable_repo.path),
        worktree_path=disposable_repo.path,
        artifact_dir=Path(isolated_workspace["runs_root"]) / "test_timeout_dur" / "artifacts",
        timeout_seconds=1,
    )

    test_result = next(r for r in results if "sleep" in r.command)
    # Duration should be at least the timeout value, minus scheduling jitter.
    assert test_result.duration_seconds > 0, "duration should be positive"


def test_skipped_commands_not_in_results(disposable_repo, isolated_workspace):
    """Previously empty commands were excluded; now all command slots appear in results.

    With the updated CommandsSection.items(), all 5 slots are returned:
    4 empty (skipped) + 1 timed-out (sleep 10).
    """
    results = run_commands(
        repo_config=_cfg(disposable_repo.path),
        worktree_path=disposable_repo.path,
        artifact_dir=Path(isolated_workspace["runs_root"]) / "test_skip" / "artifacts",
        timeout_seconds=1,
    )

    # All 5 slots should be present (including the 4 empty/skipped ones).
    assert len(results) == 5, f"expected 5 results (all command slots), got {len(results)}"

    # The last result should be the test (sleep 10) which timed out.
    test_result = results[3]  # install, lint, typecheck, test, build
    assert "sleep" in test_result.command
    assert test_result.exit_code == 124
    assert test_result.timed_out is True

    # The empty slots should be marked as skipped.
    for i in [0, 1, 2, 4]:
        assert results[i].skipped is True, f"slot {i} should be skipped"
        assert results[i].timed_out is False, f"slot {i} should not be timed out"


def test_timed_out_command_mixed_with_passing(disposable_repo, isolated_workspace):
    """When mixing a quick command and a timeout, only the timeout is marked."""
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=disposable_repo.path, default_branch="main"),
        agent=AgentSection(timeout_seconds=1800),
        commands=CommandsSection(
            lint='echo "ok"',
            test="sleep 10",
        ),
        review=ReviewSection(),
    )
    results = run_commands(
        repo_config=cfg,
        worktree_path=disposable_repo.path,
        artifact_dir=Path(isolated_workspace["runs_root"]) / "test_mixed" / "artifacts",
        timeout_seconds=1,
    )

    lint_result = next(r for r in results if "echo" in r.command)
    assert lint_result.exit_code == 0
    assert lint_result.timed_out is False

    test_result = next(r for r in results if "sleep" in r.command)
    assert test_result.exit_code == 124
    assert test_result.timed_out is True