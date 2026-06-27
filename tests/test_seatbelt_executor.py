"""v0.9.0 (Step 4) tests — macOS seatbelt executor.

Tests cover:
  - Platform check (non-macOS raises SeatbeltNotAvailableError)
  - Sandbox profile generation (filesystem + network rules)
  - Config validation (backend="seatbelt" accepted, agent required)
  - Custom profile path loading
  - Error handling (missing prompt, missing sandbox-exec)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from acp.config import ExecutorSection
from acp.executor.seatbelt import SeatbeltExecutor, SeatbeltNotAvailableError


def _seatbelt_config(**overrides) -> ExecutorSection:
    defaults = {
        "backend": "seatbelt",
        "agent": "echo hello",
        "network_policy": "locked_down",
    }
    defaults.update(overrides)
    return ExecutorSection(**defaults)


def test_seatbelt_backend_accepted_in_config():
    """The 'seatbelt' backend is accepted by ExecutorSection validation."""
    cfg = _seatbelt_config()
    assert cfg.backend == "seatbelt"


def test_seatbelt_check_installed_non_macos():
    """check_installed() returns False on non-macOS platforms."""
    with patch("acp.executor.seatbelt.sys.platform", "linux"):
        assert SeatbeltExecutor.check_installed() is False


def test_seatbelt_validate_raises_on_non_macos():
    """_validate() raises SeatbeltNotAvailableError on non-macOS."""
    executor = SeatbeltExecutor(_seatbelt_config())
    with patch("acp.executor.seatbelt.sys.platform", "linux"):
        with pytest.raises(SeatbeltNotAvailableError, match="requires macOS"):
            executor._validate()


def test_seatbelt_validate_raises_when_sandbox_exec_missing():
    """_validate() raises when sandbox-exec is not on PATH (macOS but missing)."""
    executor = SeatbeltExecutor(_seatbelt_config())
    with (
        patch("acp.executor.seatbelt.sys.platform", "darwin"),
        patch("shutil.which", return_value=None),
    ):
        with pytest.raises(SeatbeltNotAvailableError, match="sandbox-exec"):
            executor._validate()


def test_seatbelt_validate_requires_agent_command():
    """_validate() raises AgentConfigError when executor.agent is empty."""
    from acp.errors import AgentConfigError

    executor = SeatbeltExecutor(_seatbelt_config(agent=""))
    with (
        patch("acp.executor.seatbelt.sys.platform", "darwin"),
        patch("shutil.which", return_value="/usr/bin/sandbox-exec"),
    ):
        with pytest.raises(AgentConfigError, match="executor.agent is required"):
            executor._validate()


def test_seatbelt_backend_name():
    """backend_name property returns 'seatbelt'."""
    executor = SeatbeltExecutor(_seatbelt_config())
    assert executor.backend_name == "seatbelt"


def test_seatbelt_environment_info():
    """get_environment_info returns the expected metadata."""
    executor = SeatbeltExecutor(_seatbelt_config())
    info = executor.get_environment_info()
    assert info["backend"] == "seatbelt"
    assert info["network_policy"] == "locked_down"
    assert info["profile"] == "auto-generated"


def test_seatbelt_environment_info_custom_profile():
    """get_environment_info reports 'custom' when a profile path is set."""
    executor = SeatbeltExecutor(_seatbelt_config(seatbelt_profile_path="/custom/profile.sb"))
    info = executor.get_environment_info()
    assert info["profile"] == "custom"


def test_seatbelt_profile_generation_locked_down():
    """Generated profile denies network when network_policy='locked_down'."""
    executor = SeatbeltExecutor(_seatbelt_config(network_policy="locked_down"))
    worktree = Path("/tmp/test_wt")
    repo = Path("/tmp/test_repo")
    artifacts = Path("/tmp/test_artifacts")
    profile = executor._generate_profile(worktree, repo, artifacts)

    # Should deny network.
    assert "(deny network*)" in profile
    # Should allow process execution.
    assert "(allow process*)" in profile
    # Should include the worktree path for read and write.
    assert str(worktree.resolve()) in profile
    # Should include system read paths.
    assert "/usr" in profile
    assert "/bin" in profile
    # Should deny by default.
    assert "(deny default)" in profile


def test_seatbelt_profile_generation_open_network():
    """Generated profile allows network when network_policy='open'."""
    executor = SeatbeltExecutor(_seatbelt_config(network_policy="open"))
    worktree = Path("/tmp/test_wt")
    repo = Path("/tmp/test_repo")
    artifacts = Path("/tmp/test_artifacts")
    profile = executor._generate_profile(worktree, repo, artifacts)

    assert "(allow network*)" in profile
    assert "(deny network*)" not in profile


def test_seatbelt_profile_generation_balanced_network():
    """Balanced policy allows outbound+DNS but denies inbound — not identical to open."""
    executor = SeatbeltExecutor(_seatbelt_config(network_policy="balanced"))
    worktree = Path("/tmp/test_wt")
    repo = Path("/tmp/test_repo")
    artifacts = Path("/tmp/test_artifacts")
    profile = executor._generate_profile(worktree, repo, artifacts)

    assert "(allow network-outbound*)" in profile
    assert "(allow network-DNS)" in profile
    assert "(deny network-inbound*)" in profile
    # Must NOT be identical to "open"
    assert "(allow network*)" not in profile


def test_seatbelt_profile_escapes_paths_with_quotes():
    """Paths containing double quotes are escaped in the seatbelt profile."""
    executor = SeatbeltExecutor(_seatbelt_config())
    # Path with a double quote — must be escaped to prevent profile injection.
    evil_injection = '(allow file-write* (subpath "/sneaky"))'
    worktree = Path(f'/tmp/evil"; {evil_injection}')
    repo = Path("/tmp/test_repo")
    artifacts = Path("/tmp/test_artifacts")
    profile = executor._generate_profile(worktree, repo, artifacts)

    # The raw unescaped injection must NOT appear in the profile.
    assert evil_injection not in profile
    # The escaped version should be present.
    assert '\\"' in profile


def test_seatbelt_profile_generation_includes_worktree_writes():
    """Generated profile allows writes to the worktree path."""
    executor = SeatbeltExecutor(_seatbelt_config())
    worktree = Path("/tmp/test_wt")
    repo = Path("/tmp/test_repo")
    artifacts = Path("/tmp/test_artifacts")
    profile = executor._generate_profile(worktree, repo, artifacts)

    wt_resolved = str(worktree.resolve())
    # Should have file-write* for the worktree.
    assert f'(allow file-write* (subpath "{wt_resolved}"))' in profile


def test_seatbelt_profile_generation_includes_artifacts_writes():
    """Generated profile allows writes to the artifact directory."""
    executor = SeatbeltExecutor(_seatbelt_config())
    worktree = Path("/tmp/test_wt")
    repo = Path("/tmp/test_repo")
    artifacts = Path("/tmp/test_artifacts")
    profile = executor._generate_profile(worktree, repo, artifacts)

    artifacts_resolved = str(artifacts.resolve())
    assert f'(allow file-write* (subpath "{artifacts_resolved}"))' in profile


def test_seatbelt_stop_returns_true_when_no_process():
    """stop() returns True when no process is running."""
    executor = SeatbeltExecutor(_seatbelt_config())
    assert executor.stop() is True


def test_seatbelt_cleanup_no_op_when_no_profile():
    """cleanup() is a no-op when no profile was generated."""
    executor = SeatbeltExecutor(_seatbelt_config())
    executor.cleanup()  # should not raise


def test_seatbelt_cleanup_removes_temp_profile():
    """cleanup() removes a generated temp profile."""
    executor = SeatbeltExecutor(_seatbelt_config())
    # Simulate a generated profile.
    fd, path = tempfile.mkstemp(suffix=".sb", prefix="acp_test_")
    os_write = __import__("os").write
    os_write(fd, b"(version 1)\n")
    __import__("os").close(fd)
    executor._profile_path = Path(path)
    assert Path(path).exists()
    executor.cleanup()
    assert not Path(path).exists()


def test_seatbelt_cleanup_keeps_custom_profile():
    """cleanup() does NOT remove a custom profile (operator's file)."""
    executor = SeatbeltExecutor(_seatbelt_config(seatbelt_profile_path="/custom/profile.sb"))
    # Simulate a custom profile path (file doesn't need to exist for this test).
    fd, path = tempfile.mkstemp(suffix=".sb", prefix="acp_test_")
    __import__("os").write(fd, b"(version 1)\n")
    __import__("os").close(fd)
    executor._profile_path = Path(path)
    executor.cleanup()
    # Custom profile should NOT be deleted.
    assert Path(path).exists()
    Path(path).unlink(missing_ok=True)


async def test_seatbelt_start_error_on_missing_prompt(tmp_path: Path):
    """start() returns an error result when the prompt file can't be read."""
    executor = SeatbeltExecutor(_seatbelt_config())
    with (
        patch("acp.executor.seatbelt.sys.platform", "darwin"),
        patch("shutil.which", return_value="/usr/bin/sandbox-exec"),
    ):
        result = await executor.start(
            task_id="test_task",
            prompt_path=tmp_path / "nonexistent_prompt.md",
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
    assert result.exit_code == 127
    assert "cannot read prompt" in result.stderr_path.read_text()


async def test_seatbelt_start_error_on_missing_custom_profile(tmp_path: Path):
    """start() returns an error when a custom profile path doesn't exist."""
    # Create a prompt file so we get past the prompt check.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("test prompt")

    executor = SeatbeltExecutor(
        _seatbelt_config(seatbelt_profile_path=str(tmp_path / "nonexistent.sb"))
    )
    with (
        patch("acp.executor.seatbelt.sys.platform", "darwin"),
        patch("shutil.which", return_value="/usr/bin/sandbox-exec"),
    ):
        result = await executor.start(
            task_id="test_task",
            prompt_path=prompt,
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
    assert result.exit_code == 127
    assert "custom profile not found" in result.stderr_path.read_text()


def test_seatbelt_fetch_remote_returns_empty():
    """fetch_remote() returns empty string (no remote for seatbelt)."""
    executor = SeatbeltExecutor(_seatbelt_config())
    assert executor.fetch_remote() == ""
