"""Tests for Phase 2-4 features: SQLite integrity, autonomous mode,
custom secret regexes, gVisor executor, and API endpoints.

Tests the features implemented in the engineering execution plan:

Phase 2.1: SQLite integrity breach detection
Phase 2.2: Autonomous mode — repair loop aborted event + risk factors
Phase 3.2: Custom secret regexes in ReviewSection
Phase 3.1: GvisorExecutor protocol compliance
Phase 4.1: API endpoints for missions and skills
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from acp.config import ExecutorSection, ReviewSection
from acp.models import EventType, TaskStatus
from acp.review.secret_scanner import (
    detect_hard_block_secrets,
    scan_diff,
    scan_patch,
)

# --------------------------------------------------------------------------- #
# Phase 3.2: Custom secret regexes
# --------------------------------------------------------------------------- #


def test_custom_regexes_config_validation():
    """ReviewSection accepts and validates custom_secret_regexes."""
    cfg = ReviewSection(
        custom_secret_regexes=[
            {"name": "internal_api_key", "pattern": r"IAK-[A-Z0-9]{32}"},
        ],
    )
    assert len(cfg.custom_secret_regexes) == 1
    assert cfg.custom_secret_regexes[0]["name"] == "internal_api_key"


def test_custom_regexes_config_rejects_missing_keys():
    """ReviewSection rejects entries missing 'name' or 'pattern'."""
    with pytest.raises(ValueError, match="must have 'name' and 'pattern'"):
        ReviewSection(custom_secret_regexes=[{"name": "test"}])


def test_custom_regexes_config_rejects_invalid_regex():
    """ReviewSection rejects invalid regex patterns."""
    with pytest.raises(ValueError, match="invalid regex"):
        ReviewSection(
            custom_secret_regexes=[
                {"name": "bad", "pattern": "[unclosed"},
            ],
        )


def test_custom_regexes_config_rejects_empty_name():
    """ReviewSection rejects entries with empty name."""
    with pytest.raises(ValueError, match="must not be empty"):
        ReviewSection(
            custom_secret_regexes=[{"name": "", "pattern": "test"}],
        )


def test_scan_patch_with_custom_regexes():
    """scan_patch detects custom regex matches."""
    import re

    custom = [("custom:internal_key", re.compile(r"IAK-[A-Z0-9]{32}"))]
    patch = '+API_KEY = "IAK-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"\n'
    findings = scan_patch(patch, custom_regexes=custom)
    kinds = [f.kind for f in findings]
    assert "custom:internal_key" in kinds


def test_scan_patch_without_custom_regexes():
    """scan_patch works normally without custom regexes."""
    # Use a real GitHub PAT format: ghp_ followed by 36+ alphanumeric chars
    patch = '+token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"\n'
    findings = scan_patch(patch)
    kinds = [f.kind for f in findings]
    # Should still find built-in patterns
    assert "github_pat" in kinds


def test_detect_hard_block_secrets_with_custom_regex():
    """detect_hard_block_secrets treats custom regex matches as hard blocks."""
    import re

    custom = [("custom:company_token", re.compile(r"CT-[A-Z0-9]{40}"))]
    # The token must be 40 chars after CT-: ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 = 36... need 40
    token = "CT-" + "A" * 40
    patch = f'+TOKEN = "{token}"\n'
    findings = detect_hard_block_secrets(patch, custom_regexes=custom)
    assert len(findings) == 1
    assert findings[0].kind == "custom:company_token"


def test_scan_diff_with_custom_regexes():
    """scan_diff passes custom regexes through to scan_patch."""
    import re

    custom = [("custom:jwt_internal", re.compile(r"INTJWT-[A-Z0-9]{20}"))]
    patch = '+jwt = "INTJWT-ABCDEFGHIJKLMNOPQRST"\n'
    findings = scan_diff(patch, custom_regexes=custom, use_trufflehog=False)
    kinds = [f.kind for f in findings]
    assert "custom:jwt_internal" in kinds


def test_custom_regexes_in_review_config():
    """Full ReviewSection with custom regexes works in config."""
    cfg = ReviewSection(
        custom_secret_regexes=[
            {"name": "db_password", "pattern": r"DBPWD_[a-zA-Z0-9]{16}"},
        ],
    )
    assert cfg.custom_secret_regexes[0]["pattern"] == r"DBPWD_[a-zA-Z0-9]{16}"


# --------------------------------------------------------------------------- #
# Phase 2.1: SQLite integrity breach detection
# --------------------------------------------------------------------------- #


def test_store_integrity_breach_event_type_exists():
    """The STORE_INTEGRITY_BREACH event type should exist."""
    assert EventType.STORE_INTEGRITY_BREACH.value == "store.integrity_breach"


def test_durable_task_store_check_integrity_no_breach(tmp_path):
    """check_integrity returns no breaches when task.json and SQLite agree."""
    from acp.evidence.durable_task_store import DurableTaskStore
    from acp.models import Task

    db = DurableTaskStore(tmp_path / "events.db")
    db.init()

    # Create a task in both stores.
    task = Task(
        task_id="task_001",
        repo_name="demo",
        repo_path=str(tmp_path),
        base_branch="main",
        task_branch="task-001",
        worktree_path=str(tmp_path / "wt"),
        user_request="test",
        status=TaskStatus.PASSED,
    )
    db.save(task)

    # Write task.json matching the SQLite status.
    runs_root = tmp_path / "runs"
    task_dir = runs_root / task.task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(task.model_dump_json(indent=2))

    breaches = db.check_integrity(runs_root)
    assert len(breaches) == 0
    db.close()


def test_durable_task_store_check_integrity_status_mismatch(tmp_path):
    """check_integrity detects status mismatch between task.json and SQLite."""
    from acp.evidence.durable_task_store import DurableTaskStore
    from acp.models import Task

    db = DurableTaskStore(tmp_path / "events.db")
    db.init()

    task = Task(
        task_id="task_002",
        repo_name="demo",
        repo_path=str(tmp_path),
        base_branch="main",
        task_branch="task-002",
        worktree_path=str(tmp_path / "wt"),
        user_request="test",
        status=TaskStatus.PASSED,
    )
    db.save(task)

    # Write task.json with a DIFFERENT status.
    runs_root = tmp_path / "runs"
    task_dir = runs_root / task.task_id
    task_dir.mkdir(parents=True)
    task.status = TaskStatus.FAILED
    (task_dir / "task.json").write_text(task.model_dump_json(indent=2))

    breaches = db.check_integrity(runs_root)
    assert len(breaches) == 1
    assert breaches[0]["task_id"] == "task_002"
    assert breaches[0]["json_status"] == "failed"
    assert breaches[0]["sqlite_status"] == "passed"
    db.close()


def test_durable_task_store_check_integrity_missing_json(tmp_path):
    """check_integrity detects missing task.json as a breach."""
    from acp.evidence.durable_task_store import DurableTaskStore
    from acp.models import Task

    db = DurableTaskStore(tmp_path / "events.db")
    db.init()

    task = Task(
        task_id="task_003",
        repo_name="demo",
        repo_path=str(tmp_path),
        base_branch="main",
        task_branch="task-003",
        worktree_path=str(tmp_path / "wt"),
        user_request="test",
        status=TaskStatus.PASSED,
    )
    db.save(task)

    # No task.json written — should be detected as a breach.
    runs_root = tmp_path / "runs"
    breaches = db.check_integrity(runs_root)
    assert len(breaches) == 1
    assert breaches[0]["json_status"] == "(missing)"
    db.close()


def test_durable_task_store_check_integrity_callback(tmp_path):
    """check_integrity calls on_breach callback when a breach is found."""
    from acp.evidence.durable_task_store import DurableTaskStore
    from acp.models import Task

    db = DurableTaskStore(tmp_path / "events.db")
    db.init()

    task = Task(
        task_id="task_004",
        repo_name="demo",
        repo_path=str(tmp_path),
        base_branch="main",
        task_branch="task-004",
        worktree_path=str(tmp_path / "wt"),
        user_request="test",
        status=TaskStatus.PASSED,
    )
    db.save(task)

    runs_root = tmp_path / "runs"
    task_dir = runs_root / task.task_id
    task_dir.mkdir(parents=True)
    task.status = TaskStatus.NEEDS_REVIEW
    (task_dir / "task.json").write_text(task.model_dump_json(indent=2))

    callback_calls = []

    def on_breach(task_id, json_status, sqlite_status):
        callback_calls.append((task_id, json_status, sqlite_status))

    db.check_integrity(runs_root, on_breach=on_breach)
    assert len(callback_calls) == 1
    assert callback_calls[0][0] == "task_004"
    db.close()


# --------------------------------------------------------------------------- #
# Phase 2.2: Autonomous mode — repair loop aborted event
# --------------------------------------------------------------------------- #


def test_auto_repair_loop_aborted_event_type_exists():
    """The AUTO_REPAIR_LOOP_ABORTED event type should exist."""
    assert EventType.AUTO_REPAIR_LOOP_ABORTED.value == "auto.repair_loop_aborted"


def test_auto_merge_refused_includes_risk_factors():
    """AUTO_MERGE_REFUSED event should include risk_factors in the payload.

    This is a structural test — we verify the event type exists and
    the autonomous_merge_node code includes risk_factors. Full E2E
    testing of autonomous mode is in test_autonomous_mode.py.
    """
    # Verify the event type exists.
    assert EventType.AUTO_MERGE_REFUSED.value == "auto.merge.refused"

    # Verify the autonomous_merge_node function references risk_factors
    # by checking the source.
    import acp.graph.nodes as nodes_mod

    source = open(nodes_mod.__file__).read()
    assert "risk_factors" in source, (
        "autonomous_merge_node should include risk_factors in the AUTO_MERGE_REFUSED event payload"
    )


# --------------------------------------------------------------------------- #
# Phase 3.1: GvisorExecutor
# --------------------------------------------------------------------------- #


def test_gvisor_executor_backend_name():
    """GvisorExecutor.backend_name returns 'gvisor'."""
    from acp.executor.gvisor import GvisorExecutor

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent="claude"))
    assert executor.backend_name == "gvisor"


def test_gvisor_executor_protocol_compliance():
    """GvisorExecutor satisfies the Executor protocol."""
    from acp.executor.gvisor import GvisorExecutor
    from acp.executor.protocol import Executor

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent="claude"))
    assert isinstance(executor, Executor)


def test_gvisor_executor_check_installed_returns_false_without_docker():
    """GvisorExecutor.check_installed returns False when Docker is missing."""
    from acp.executor.gvisor import GvisorExecutor

    with patch("shutil.which", return_value=None):
        assert GvisorExecutor.check_installed() is False


def test_gvisor_executor_check_installed_returns_false_without_runsc():
    """GvisorExecutor.check_installed returns False when runsc is not registered."""
    from acp.executor.gvisor import GvisorExecutor

    mock_proc = MagicMock(stdout='{"runc":{}}', returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("subprocess.run", return_value=mock_proc),
    ):
        assert GvisorExecutor.check_installed() is False


def test_gvisor_executor_check_installed_returns_true_with_runsc():
    """GvisorExecutor.check_installed returns True when runsc is registered."""
    from acp.executor.gvisor import GvisorExecutor

    mock_proc = MagicMock(stdout='{"runsc":{"path":"/usr/bin/runsc"}}', returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("subprocess.run", return_value=mock_proc),
    ):
        assert GvisorExecutor.check_installed() is True


def test_gvisor_executor_validate_fails_without_docker():
    """GvisorExecutor._validate raises when Docker/gVisor not installed."""
    from acp.executor.gvisor import GvisorExecutor, GvisorNotInstalledError

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent="claude"))
    with patch.object(GvisorExecutor, "check_installed", return_value=False):
        with pytest.raises(GvisorNotInstalledError):
            executor._validate()


def test_gvisor_executor_validate_fails_without_agent():
    """GvisorExecutor._validate raises when agent is not set."""
    from acp.executor.gvisor import AgentConfigError, GvisorExecutor

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent=""))
    with patch.object(GvisorExecutor, "check_installed", return_value=True):
        with pytest.raises(AgentConfigError, match="agent is required"):
            executor._validate()


def test_gvisor_executor_validate_fails_without_clone_mode():
    """GvisorExecutor._validate raises when clone_mode is False."""
    from acp.executor.gvisor import AgentConfigError, GvisorExecutor

    executor = GvisorExecutor(
        ExecutorSection(backend="gvisor", agent="claude", clone_mode=False),
    )
    with patch.object(GvisorExecutor, "check_installed", return_value=True):
        with pytest.raises(AgentConfigError, match="clone_mode must be True"):
            executor._validate()


def test_gvisor_executor_stop_returns_false_without_container():
    """GvisorExecutor.stop returns False when no container was started."""
    from acp.executor.gvisor import GvisorExecutor

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent="claude"))
    assert executor.stop() is False


def test_gvisor_executor_remove_returns_false_without_container():
    """GvisorExecutor.remove returns False when no container was started."""
    from acp.executor.gvisor import GvisorExecutor

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent="claude"))
    assert executor.remove() is False


def test_gvisor_executor_info():
    """GvisorExecutor.info returns metadata dict."""
    from acp.executor.gvisor import GvisorExecutor

    executor = GvisorExecutor(
        ExecutorSection(backend="gvisor", agent="claude", network_policy="locked_down"),
    )
    info = executor.info()
    assert info["backend"] == "gvisor"
    assert info["agent"] == "claude"
    assert info["network_policy"] == "locked_down"


def test_gvisor_executor_fetch_remote_returns_empty():
    """GvisorExecutor.fetch_remote returns empty (volume-mounted worktree)."""
    from acp.executor.gvisor import GvisorExecutor

    executor = GvisorExecutor(ExecutorSection(backend="gvisor", agent="claude"))
    assert executor.fetch_remote(Path("/tmp")) == ""


def test_gvisor_executor_config_accepts_gvisor():
    """ExecutorSection accepts 'gvisor' as a backend."""
    cfg = ExecutorSection(backend="gvisor", agent="claude")
    assert cfg.backend == "gvisor"


# --------------------------------------------------------------------------- #
# Phase 4.1: API endpoints (missions + skills)
# --------------------------------------------------------------------------- #


def test_missions_endpoint_exists():
    """GET /missions endpoint is registered in the FastAPI app."""
    try:
        from acp.api.server import app

        routes = [r.path for r in app.routes]
        assert "/missions" in routes, "GET /missions endpoint not found"
        assert "/missions/{mission_id}" in routes, "GET /missions/{mission_id} not found"
    except ImportError:
        pytest.skip("api extra not installed")


def test_skills_endpoint_exists():
    """GET /skills endpoint is registered in the FastAPI app."""
    try:
        from acp.api.server import app

        routes = [r.path for r in app.routes]
        assert "/skills" in routes, "GET /skills endpoint not found"
    except ImportError:
        pytest.skip("api extra not installed")
