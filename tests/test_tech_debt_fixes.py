"""Tests for the technical debt cleanup and stabilization fixes.

Tests the immediate action items from the comprehensive codebase review:

  1. Context __init__.py docstring updated (no longer says "stub")
  2. RiskEngine.recommend() respects require_human_approval
  3. remove_worktree logs errors instead of silent except: pass
  4. _cleanup_sandbox logs errors instead of silent except: pass
  5. lifecycle.py logs errors instead of silent except: pass
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from acp.models import Recommendation, RiskLevel
from acp.review.risk import RiskCategory, RiskEngine

# --------------------------------------------------------------------------- #
# 1. Context docstring (structural test — verifies the docstring is updated)
# --------------------------------------------------------------------------- #


def test_context_docstring_does_not_say_stub():
    """The context __init__.py docstring should not say 'stub'."""
    from acp.context import __doc__ as context_doc

    assert context_doc is not None
    # The outdated docstring said "prompt-only stub"
    assert "prompt-only stub" not in context_doc, (
        "context/__init__.py docstring still says 'prompt-only stub' — "
        "Milestone 6 (Haystack retrieval) has been fully implemented"
    )


def test_context_docstring_mentions_haystack():
    """The context __init__.py docstring should mention Haystack retrieval."""
    from acp.context import __doc__ as context_doc

    assert context_doc is not None
    assert "Haystack" in context_doc or "haystack" in context_doc


# --------------------------------------------------------------------------- #
# 2. RiskEngine.recommend() respects require_human_approval
# --------------------------------------------------------------------------- #


def test_risk_engine_revise_when_human_approval_required():
    """LOW risk + tests pass + require_human_approval → REVISE (not MERGE)."""
    engine = RiskEngine()
    rec = engine.recommend(tests_pass=True, require_human_approval=True)
    assert rec == Recommendation.REVISE


def test_risk_engine_merge_when_human_approval_not_required():
    """LOW risk + tests pass + no human approval required → MERGE."""
    engine = RiskEngine()
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.MERGE


def test_risk_engine_reject_on_hard_block_regardless_of_approval():
    """Hard block → REJECT regardless of require_human_approval."""
    engine = RiskEngine()
    engine.add(RiskCategory.SECRET, "secret", level=RiskLevel.HIGH, hard_block=True)
    rec = engine.recommend(tests_pass=True, require_human_approval=True)
    assert rec == Recommendation.REJECT
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.REJECT


def test_risk_engine_reject_on_high_risk_regardless_of_approval():
    """HIGH risk → REJECT regardless of require_human_approval."""
    engine = RiskEngine()
    engine.add(RiskCategory.QUANTITY, "massive diff", level=RiskLevel.HIGH)
    rec = engine.recommend(tests_pass=True, require_human_approval=True)
    assert rec == Recommendation.REJECT
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.REJECT


def test_risk_engine_revise_on_medium_risk_regardless_of_approval():
    """MEDIUM risk → REVISE regardless of require_human_approval."""
    engine = RiskEngine()
    engine.add(RiskCategory.AUTH, "auth file", level=RiskLevel.MEDIUM)
    rec = engine.recommend(tests_pass=True, require_human_approval=True)
    assert rec == Recommendation.REVISE
    rec = engine.recommend(tests_pass=True, require_human_approval=False)
    assert rec == Recommendation.REVISE


def test_risk_engine_reject_on_failing_tests_regardless_of_approval():
    """Failing tests → REJECT regardless of require_human_approval."""
    engine = RiskEngine()
    rec = engine.recommend(tests_pass=False, require_human_approval=True)
    assert rec == Recommendation.REJECT
    rec = engine.recommend(tests_pass=False, require_human_approval=False)
    assert rec == Recommendation.REJECT


# --------------------------------------------------------------------------- #
# 3. remove_worktree logs errors (not silent)
# --------------------------------------------------------------------------- #


def test_remove_worktree_logs_error(tmp_path):
    """remove_worktree logs a warning when git worktree remove fails."""
    # Create a fake repo so Repo() doesn't fail.
    import subprocess

    from acp.gitops.worktrees import remove_worktree

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(repo_path),
        capture_output=True,
    )

    # Try to remove a non-existent worktree — should log, not crash.
    with patch("acp.gitops.worktrees.logger") as mock_logger:
        remove_worktree(repo_path, tmp_path / "nonexistent", force=True)
        # The warning should have been called (worktree remove fails on
        # non-existent path, but prune still runs).
        assert mock_logger.warning.called, (
            "remove_worktree should log a warning when git worktree remove fails"
        )


# --------------------------------------------------------------------------- #
# 4. _cleanup_sandbox logs errors (not silent)
# --------------------------------------------------------------------------- #


async def test_cleanup_sandbox_logs_error(caplog):
    """_cleanup_sandbox logs a warning when sandbox cleanup fails."""
    import logging

    from acp.config import ExecutorSection
    from acp.events import EventWriter
    from acp.graph.nodes import NodeContext, _cleanup_sandbox
    from acp.store import TaskStore

    # This is hard to test in isolation because _cleanup_sandbox needs
    # a full NodeContext and state. We test that the logger is called
    # by patching SbxExecutor to raise during cleanup.
    cfg = MagicMock()
    cfg.executor.backend = "docker_sbx"
    cfg.executor = ExecutorSection(backend="docker_sbx", agent="claude")

    state = {
        "config": cfg,
        "sandbox_name": "test-sandbox",
        "sandbox_remote": "sandbox://test",
    }

    store = TaskStore(runs_root="/tmp/acp_test_cleanup")
    events = EventWriter("__test__", store.root / "__test__")
    ctx = NodeContext(store=store, events=events)

    with patch("acp.graph.nodes.SbxExecutor") as mock_sbx:
        mock_instance = mock_sbx.return_value
        mock_instance.cleanup.side_effect = RuntimeError("docker teardown failed")
        with caplog.at_level(logging.WARNING, logger="acp.graph.nodes"):
            await _cleanup_sandbox(state, ctx)

    # Verify the warning was logged.
    assert any("sandbox cleanup failed" in record.message for record in caplog.records), (
        "_cleanup_sandbox should log a warning when cleanup fails"
    )


# --------------------------------------------------------------------------- #
# 5. lifecycle.py logs errors (not silent) — structural verification
# --------------------------------------------------------------------------- #


def test_lifecycle_module_has_logger():
    """lifecycle.py should have a module-level logger."""
    import acp.evidence.lifecycle as lifecycle_mod

    assert hasattr(lifecycle_mod, "logger"), (
        "lifecycle.py should have a module-level logger for error reporting"
    )


def test_worktrees_module_has_logger():
    """worktrees.py should have a module-level logger."""
    import acp.gitops.worktrees as worktrees_mod

    assert hasattr(worktrees_mod, "logger"), (
        "worktrees.py should have a module-level logger for error reporting"
    )


def test_nodes_module_has_logger():
    """nodes.py should have a module-level logger."""
    import acp.graph.nodes as nodes_mod

    assert hasattr(nodes_mod, "logger"), (
        "nodes.py should have a module-level logger for error reporting"
    )
