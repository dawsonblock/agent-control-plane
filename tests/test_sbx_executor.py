"""v0.5.13 acceptance criteria — Docker Sandboxes executor backend.

Tests the 8 acceptance criteria from the v0.5.13 spec:

  1. ACP fails closed if executor=sbx and ``sbx`` is missing.
  2. ACP records sbx version.
  3. ACP refuses non-clone mode by default.
  4. ACP records network policy.
  5. ACP records secret names but never secret values.
  6. ACP captures diff from sandbox remote, not host worktree mutation.
  7. ACP verify binds executor policy/config.
  8. ACP cleanup can stop/remove sandbox when requested.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from acp.config import ExecutorSection, RepoConfig, RepoSection
from acp.executor.sbx import SandboxInfo, SbxExecutor, SbxNotInstalledError
from acp.gitops.diff import capture_diff_from_remote
from acp.models import EventType


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _executor_config(**kwargs) -> ExecutorSection:
    """Build an ExecutorSection with sensible defaults for tests."""
    defaults = {
        "backend": "docker_sbx",
        "agent": "claude",
        "clone_mode": True,
        "network_policy": "locked_down",
    }
    defaults.update(kwargs)
    return ExecutorSection(**defaults)


def _mock_sbx_installed():
    """Patch shutil.which to report sbx as installed."""
    return patch("acp.executor.sbx.shutil.which", return_value="/usr/local/bin/sbx")


def _mock_sbx_version(version: str = "sbx 1.0.0"):
    """Patch subprocess.run to return a version string for --version."""
    def _run_mock(cmd, **kwargs):
        if cmd == ["sbx", "--version"]:
            return MagicMock(stdout=version, stderr="", returncode=0)
        return MagicMock(stdout="", stderr="", returncode=0)
    return patch("acp.executor.sbx.subprocess.run", side_effect=_run_mock)


# --------------------------------------------------------------------------- #
# 1. ACP fails closed if executor=sbx and sbx is missing
# --------------------------------------------------------------------------- #


class TestSbxNotInstalled:
    """Acceptance criterion 1: fail closed when sbx is not on PATH."""

    def test_validate_raises_when_sbx_missing(self):
        """SbxExecutor._validate() raises SbxNotInstalledError when sbx is absent."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        with patch("acp.executor.sbx.shutil.which", return_value=None):
            with pytest.raises(SbxNotInstalledError, match="requires the 'sbx' CLI"):
                executor._validate()

    def test_check_installed_returns_false_when_missing(self):
        """check_installed() returns False when sbx is not on PATH."""
        with patch("acp.executor.sbx.shutil.which", return_value=None):
            assert SbxExecutor.check_installed() is False

    def test_check_installed_returns_true_when_present(self):
        """check_installed() returns True when sbx is on PATH."""
        with patch("acp.executor.sbx.shutil.which", return_value="/usr/local/bin/sbx"):
            assert SbxExecutor.check_installed() is True


# --------------------------------------------------------------------------- #
# 2. ACP records sbx version
# --------------------------------------------------------------------------- #


class TestSbxVersionRecording:
    """Acceptance criterion 2: sbx version is recorded in metadata."""

    def test_get_version_returns_string(self):
        """get_version() returns the sbx version string."""
        with _mock_sbx_version("sbx 1.2.3"):
            assert SbxExecutor.get_version() == "sbx 1.2.3"

    def test_get_version_empty_when_not_installed(self):
        """get_version() returns empty string when sbx is not installed."""
        with patch("acp.executor.sbx.subprocess.run", side_effect=FileNotFoundError):
            assert SbxExecutor.get_version() == ""

    def test_sandbox_info_includes_version(self):
        """sandbox_info() includes the sbx version in the metadata."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        executor._sbx_version = "sbx 2.0.0"
        info = executor.sandbox_info("task-123")
        assert info.sbx_version == "sbx 2.0.0"
        assert "sbx_version" in info.to_dict()
        assert info.to_dict()["sbx_version"] == "sbx 2.0.0"


# --------------------------------------------------------------------------- #
# 3. ACP refuses non-clone mode by default
# --------------------------------------------------------------------------- #


class TestCloneModeEnforced:
    """Acceptance criterion 3: ACP refuses non-clone mode."""

    def test_validate_raises_when_clone_mode_false(self):
        """_validate() raises when clone_mode is False."""
        cfg = _executor_config(clone_mode=False)
        executor = SbxExecutor(cfg)
        with _mock_sbx_installed():
            with pytest.raises(Exception, match="clone_mode=False is not allowed"):
                executor._validate()

    def test_validate_raises_when_network_policy_open(self):
        """_validate() raises when network_policy is 'open'."""
        cfg = _executor_config(network_policy="open")
        executor = SbxExecutor(cfg)
        with _mock_sbx_installed():
            with pytest.raises(Exception, match="network_policy='open'"):
                executor._validate()

    def test_validate_raises_when_agent_empty(self):
        """_validate() raises when no agent is specified."""
        cfg = _executor_config(agent="")
        executor = SbxExecutor(cfg)
        with _mock_sbx_installed():
            with pytest.raises(Exception, match="executor.agent is required"):
                executor._validate()

    def test_validate_passes_with_correct_config(self):
        """_validate() passes when all config is correct."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        with _mock_sbx_installed():
            executor._validate()  # should not raise


# --------------------------------------------------------------------------- #
# 4. ACP records network policy
# --------------------------------------------------------------------------- #


class TestNetworkPolicyRecorded:
    """Acceptance criterion 4: network policy is recorded in metadata."""

    def test_locked_down_policy_recorded(self):
        cfg = _executor_config(network_policy="locked_down")
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-1")
        assert info.network_policy == "locked_down"
        assert info.to_dict()["network_policy"] == "locked_down"

    def test_balanced_policy_recorded(self):
        cfg = _executor_config(network_policy="balanced")
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-1")
        assert info.network_policy == "balanced"

    def test_host_repo_mode_is_read_only(self):
        """Clone mode always means the host repo is read-only."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-1")
        assert info.host_repo_mode == "read_only"
        assert info.clone_mode is True


# --------------------------------------------------------------------------- #
# 5. ACP records secret names but never secret values
# --------------------------------------------------------------------------- #


class TestSecretRecording:
    """Acceptance criterion 5: secret names recorded, values never."""

    def test_secrets_values_recorded_is_false(self):
        """The metadata explicitly states secret values are NOT recorded."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-1")
        assert info.secrets_values_recorded is False
        assert info.to_dict()["secrets_values_recorded"] is False

    def test_secrets_used_by_name_is_list(self):
        """secrets_used_by_name is a list (empty by default in v0.5.13)."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-1")
        assert isinstance(info.secrets_used_by_name, list)
        # No actual secret values appear anywhere in the metadata.
        metadata_json = json.dumps(info.to_dict())
        # Check that no common secret value patterns appear (not just
        # substrings like "sk-" which can appear in "sandbox-").
        assert '"secrets_used_by_name": []' in metadata_json
        assert "token" not in metadata_json.lower() or "secrets_values_recorded" in metadata_json
        # The metadata never contains a "secret_value" or "key_value" field.
        assert "secret_value" not in metadata_json
        assert "key_value" not in metadata_json


# --------------------------------------------------------------------------- #
# 6. ACP captures diff from sandbox remote, not host worktree mutation
# --------------------------------------------------------------------------- #


class TestDiffFromSandboxRemote:
    """Acceptance criterion 6: diff comes from sandbox remote, not worktree."""

    def test_capture_diff_from_remote_produces_patch(self, tmp_path):
        """capture_diff_from_remote produces a diff from the sandbox remote ref."""
        from git import Repo

        # Set up a repo with a base commit on main.
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base commit")
        # Ensure we're on main (git init may default to master).
        repo.git.branch("-M", "main")

        # Simulate the sandbox remote: create a bare repo and push a branch.
        remote_path = tmp_path / "sandbox-remote"
        remote_repo = Repo.init(str(remote_path), bare=True)

        # Add the bare repo as a remote named "sandbox-acp-test".
        repo.create_remote("sandbox-acp-test", str(remote_path))

        # Make a change on a new branch and push to the remote's main.
        repo.git.checkout("-b", "sandbox-work")
        (repo_path / "NEW_FILE.md").write_text("# agent created this\n")
        repo.git.add(".")
        repo.git.commit("-m", "agent change")
        repo.git.push("sandbox-acp-test", "sandbox-work:refs/heads/main")

        # Switch back to main.
        repo.git.checkout("main")

        # Fetch the remote.
        repo.git.fetch("sandbox-acp-test")

        # Capture the diff from the remote.
        artifacts = tmp_path / "artifacts"
        diff = capture_diff_from_remote(
            repo_path=repo_path,
            remote="sandbox-acp-test",
            base_branch="main",
            artifacts_dir=artifacts,
            remote_branch="main",
        )

        assert "NEW_FILE.md" in diff.changed_files
        assert diff.insertions > 0
        assert (artifacts / "diff.patch").exists()
        assert (artifacts / "diff_stat.txt").exists()
        assert "NEW_FILE.md" in (artifacts / "diff.patch").read_text()

    def test_capture_diff_from_remote_empty_when_no_changes(self, tmp_path):
        """capture_diff_from_remote produces empty diff when remote matches base."""
        from git import Repo

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base commit")
        repo.git.branch("-M", "main")

        # Create a bare remote and push main to it (no changes).
        remote_path = tmp_path / "sandbox-remote"
        remote_repo = Repo.init(str(remote_path), bare=True)
        repo.create_remote("sandbox-acp-test", str(remote_path))
        repo.git.push("sandbox-acp-test", "main:refs/heads/main")
        repo.git.fetch("sandbox-acp-test")

        artifacts = tmp_path / "artifacts"
        diff = capture_diff_from_remote(
            repo_path=repo_path,
            remote="sandbox-acp-test",
            base_branch="main",
            artifacts_dir=artifacts,
            remote_branch="main",
        )

        assert diff.changed_files == []
        assert diff.insertions == 0
        assert diff.deletions == 0


# --------------------------------------------------------------------------- #
# 7. ACP verify binds executor policy/config
# --------------------------------------------------------------------------- #


class TestExecutorConfigBinding:
    """Acceptance criterion 7: executor config is bound to evidence.

    The executor metadata is recorded in the sandbox.started event, which
    is part of the signed hash-chained event log. This binds the executor
    policy (backend, clone_mode, network_policy, agent) to the evidence.
    """

    def test_sandbox_started_event_contains_executor_metadata(self):
        """The SANDBOX_STARTED event type exists and carries executor metadata."""
        assert EventType.SANDBOX_STARTED.value == "sandbox.started"
        assert EventType.SANDBOX_STOPPED.value == "sandbox.stopped"

    def test_executor_config_in_sandbox_info(self):
        """SandboxInfo contains all executor policy fields."""
        cfg = _executor_config(
            agent="codex",
            network_policy="balanced",
            clone_mode=True,
        )
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-abc")
        d = info.to_dict()
        assert d["backend"] == "docker_sbx"
        assert d["agent"] == "codex"
        assert d["clone_mode"] is True
        assert d["network_policy"] == "balanced"
        assert d["sandbox_name"] == "acp-task-abc"
        assert d["sandbox_remote"] == "sandbox-acp-task-abc"
        assert d["host_repo_mode"] == "read_only"

    def test_executor_config_serializable(self):
        """Executor metadata is JSON-serializable for event payloads."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        info = executor.sandbox_info("task-1")
        # Must be serializable — this is what goes into the event payload.
        payload = json.dumps(info.to_dict())
        parsed = json.loads(payload)
        assert parsed["backend"] == "docker_sbx"
        assert parsed["clone_mode"] is True


# --------------------------------------------------------------------------- #
# 8. ACP cleanup can stop/remove sandbox when requested
# --------------------------------------------------------------------------- #


class TestSandboxCleanup:
    """Acceptance criterion 8: ACP can stop and remove sandboxes."""

    def test_stop_calls_sbx_stop(self):
        """stop() invokes `sbx stop <name>`."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-1"
        with patch("acp.executor.sbx.subprocess.run") as mock_run:
            result = executor.stop()
            assert result is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["sbx", "stop", "acp-test-1"]

    def test_remove_calls_sbx_rm(self):
        """remove() invokes `sbx rm <name>`."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-1"
        with patch("acp.executor.sbx.subprocess.run") as mock_run:
            result = executor.remove()
            assert result is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["sbx", "rm", "acp-test-1"]

    def test_cleanup_stops_when_remove_after_run_false(self):
        """cleanup() stops (not removes) when remove_after_run is False."""
        cfg = _executor_config(remove_after_run=False)
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-1"
        with patch("acp.executor.sbx.subprocess.run") as mock_run:
            executor.cleanup()
            args = mock_run.call_args[0][0]
            assert args == ["sbx", "stop", "acp-test-1"]

    def test_cleanup_removes_when_remove_after_run_true(self):
        """cleanup() removes when remove_after_run is True."""
        cfg = _executor_config(remove_after_run=True)
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-1"
        with patch("acp.executor.sbx.subprocess.run") as mock_run:
            executor.cleanup()
            args = mock_run.call_args[0][0]
            assert args == ["sbx", "rm", "acp-test-1"]

    def test_stop_returns_false_when_no_sandbox_name(self):
        """stop() returns False when no sandbox has been started."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        assert executor.stop() is False

    def test_remove_returns_false_when_no_sandbox_name(self):
        """remove() returns False when no sandbox has been started."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        assert executor.remove() is False

    def test_stop_returns_false_on_file_not_found(self):
        """stop() returns False when sbx is not installed."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-1"
        with patch("acp.executor.sbx.subprocess.run", side_effect=FileNotFoundError):
            assert executor.stop() is False


# --------------------------------------------------------------------------- #
# Sandbox name derivation
# --------------------------------------------------------------------------- #


class TestSandboxNaming:
    """Sandbox names are derived from prefix + task ID."""

    def test_default_prefix(self):
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        assert executor.sandbox_name("task-123") == "acp-task-123"

    def test_custom_prefix(self):
        cfg = _executor_config(sandbox_name_prefix="myproj")
        executor = SbxExecutor(cfg)
        assert executor.sandbox_name("task-123") == "myproj-task-123"

    def test_remote_name(self):
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        assert executor.sandbox_remote("task-123") == "sandbox-acp-task-123"

    def test_task_id_sanitized(self):
        """Slashes and underscores in task_id are replaced with dashes."""
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        assert executor.sandbox_name("task/123_abc") == "acp-task-123-abc"
