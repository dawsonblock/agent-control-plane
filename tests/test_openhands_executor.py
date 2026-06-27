"""Tests for v0.7.0 (Phase 2.1) — Executor protocol + OpenHands backend.

Tests the executor abstraction and OpenHands integration:

  1. Executor protocol — SbxExecutor and OpenHandsExecutor satisfy it
  2. OpenHandsExecutor — check_installed, get_version, validation
  3. OpenHandsExecutor.start — runs the openhands CLI, captures output
  4. JSONL event parsing — counting and extracting events from stdout
  5. OpenHandsInfo — metadata record for evidence
  6. Config — backend="openhands" accepted, agent field required
  7. CLI integration — acp run with --backend openhands
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from acp.config import ExecutorSection
from acp.executor import Executor, OpenHandsExecutor, SbxExecutor
from acp.executor.openhands import OpenHandsInfo, OpenHandsNotInstalledError
from acp.models import AgentResult

# --------------------------------------------------------------------------- #
# 1. Executor protocol
# --------------------------------------------------------------------------- #


def test_sbx_executor_is_executor():
    """SbxExecutor satisfies the Executor protocol."""
    cfg = ExecutorSection(backend="docker_sbx", agent="claude")
    executor = SbxExecutor(cfg)
    assert isinstance(executor, Executor)


def test_openhands_executor_is_executor():
    """OpenHandsExecutor satisfies the Executor protocol."""
    cfg = ExecutorSection(backend="openhands", agent="claude-sonnet-4-20250514")
    executor = OpenHandsExecutor(cfg)
    assert isinstance(executor, Executor)


def test_openhands_executor_backend_name():
    """OpenHandsExecutor.backend_name returns 'openhands'."""
    cfg = ExecutorSection(backend="openhands", agent="claude")
    executor = OpenHandsExecutor(cfg)
    assert executor.backend_name == "openhands"


# --------------------------------------------------------------------------- #
# 2. OpenHandsExecutor — check_installed, get_version, validation
# --------------------------------------------------------------------------- #


def test_check_installed_returns_bool():
    """check_installed returns a boolean."""
    result = OpenHandsExecutor.check_installed()
    assert isinstance(result, bool)


def test_get_version_returns_string():
    """get_version returns a string (possibly empty)."""
    version = OpenHandsExecutor.get_version()
    assert isinstance(version, str)


def test_validate_raises_when_not_installed():
    """_validate raises OpenHandsNotInstalledError when openhands is not on PATH."""
    cfg = ExecutorSection(backend="openhands", agent="claude")
    executor = OpenHandsExecutor(cfg)
    with patch.object(OpenHandsExecutor, "check_installed", return_value=False):
        with pytest.raises(OpenHandsNotInstalledError, match="requires the 'openhands' CLI"):
            executor._validate()


def test_validate_raises_when_no_agent():
    """_validate raises AgentConfigError when agent is not set."""
    cfg = ExecutorSection(backend="openhands", agent="")
    executor = OpenHandsExecutor(cfg)
    with patch.object(OpenHandsExecutor, "check_installed", return_value=True):
        with pytest.raises(Exception, match="executor.agent is required"):
            executor._validate()


def test_validate_passes_when_installed_and_agent_set():
    """_validate passes when openhands is installed and agent is set."""
    cfg = ExecutorSection(backend="openhands", agent="claude-sonnet-4-20250514")
    executor = OpenHandsExecutor(cfg)
    with patch.object(OpenHandsExecutor, "check_installed", return_value=True):
        executor._validate()  # should not raise


# --------------------------------------------------------------------------- #
# 3. OpenHandsExecutor.start — runs the CLI, captures output
# --------------------------------------------------------------------------- #


def test_start_captures_stdout_stderr(tmp_path):
    """start() captures stdout and stderr to artifact files."""
    cfg = ExecutorSection(backend="openhands", agent="test-model")
    executor = OpenHandsExecutor(cfg)

    # v0.7.4: The executor now streams stdout/stderr directly to files
    # via subprocess.run(stdout=f, stderr=f). The mock needs to write
    # to the file handles it receives.
    mock_result = MagicMock()
    mock_result.returncode = 0

    def _mock_run(*args, **kwargs):
        # Write to the file handles passed as stdout/stderr kwargs.
        stdout_f = kwargs.get("stdout")
        stderr_f = kwargs.get("stderr")
        if stdout_f is not None:
            stdout_f.write('{"type": "action", "action": "write"}\n')
        if stderr_f is not None:
            stderr_f.write("")
        return mock_result

    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("test task")

    artifact_dir = tmp_path / "artifacts"
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    with (
        patch.object(OpenHandsExecutor, "check_installed", return_value=True),
        patch.object(OpenHandsExecutor, "get_version", return_value="0.1.0"),
        patch("subprocess.run", side_effect=_mock_run),
    ):
        result = executor.start(
            task_id="task_20260626_0001",
            prompt_path=prompt_path,
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            timeout_seconds=60,
        )

    assert isinstance(result, AgentResult)
    assert result.exit_code == 0
    assert result.stdout_path.is_file()
    assert result.stderr_path.is_file()
    assert "write" in result.stdout_path.read_text()


def test_start_returns_correct_agent_name(tmp_path):
    """start() returns AgentResult with 'openhands:<model>' agent name."""
    cfg = ExecutorSection(backend="openhands", agent="claude-sonnet-4-20250514")
    executor = OpenHandsExecutor(cfg)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with (
        patch.object(OpenHandsExecutor, "check_installed", return_value=True),
        patch.object(OpenHandsExecutor, "get_version", return_value="0.1.0"),
        patch("subprocess.run", return_value=mock_result),
    ):
        result = executor.start(
            task_id="task_001",
            prompt_path=tmp_path / "prompt.txt",
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=30,
        )

    assert result.agent_name == "openhands:claude-sonnet-4-20250514"


def test_start_handles_timeout(tmp_path):
    """start() handles subprocess timeout gracefully."""
    import subprocess

    cfg = ExecutorSection(backend="openhands", agent="test-model")
    executor = OpenHandsExecutor(cfg)

    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("test")

    with (
        patch.object(OpenHandsExecutor, "check_installed", return_value=True),
        patch.object(OpenHandsExecutor, "get_version", return_value="0.1.0"),
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="openhands", timeout=5)),
    ):
        result = executor.start(
            task_id="task_001",
            prompt_path=prompt_path,
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=5,
        )

    assert result.exit_code == 124  # timeout exit code
    assert "timed out" in result.stderr_path.read_text()


def test_start_handles_not_found(tmp_path):
    """start() handles FileNotFoundError when openhands is not on PATH."""
    cfg = ExecutorSection(backend="openhands", agent="test-model")
    executor = OpenHandsExecutor(cfg)

    # check_installed returns True (so _validate passes), but subprocess.run
    # raises FileNotFoundError (race condition: openhands was uninstalled).
    with (
        patch.object(OpenHandsExecutor, "check_installed", return_value=True),
        patch.object(OpenHandsExecutor, "get_version", return_value=""),
        patch("subprocess.run", side_effect=FileNotFoundError),
    ):
        result = executor.start(
            task_id="task_001",
            prompt_path=tmp_path / "prompt.txt",
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=30,
        )

    assert result.exit_code == 127
    assert "not found" in result.stderr_path.read_text()


# --------------------------------------------------------------------------- #
# 4. JSONL event parsing
# --------------------------------------------------------------------------- #


def test_count_jsonl_events():
    """_count_jsonl_events counts valid JSON lines."""
    stdout = (
        '{"type": "action", "action": "write"}\n'
        "not json\n"
        '{"type": "observation", "content": "done"}\n'
        "\n"
        '{"type": "action", "action": "run"}\n'
    )
    count = OpenHandsExecutor._count_jsonl_events(stdout)
    assert count == 3


def test_count_jsonl_events_empty():
    """_count_jsonl_events returns 0 for empty stdout."""
    assert OpenHandsExecutor._count_jsonl_events("") == 0
    assert OpenHandsExecutor._count_jsonl_events("not json\n") == 0


def test_extract_jsonl_events(tmp_path):
    """_extract_jsonl_events writes only valid JSON lines to a file."""
    stdout = (
        '{"type": "action", "action": "write"}\n'
        "not json\n"
        '{"type": "observation", "content": "done"}\n'
    )
    output_path = tmp_path / "events.jsonl"
    OpenHandsExecutor._extract_jsonl_events(stdout, output_path)

    lines = output_path.read_text().strip().split("\n")
    assert len(lines) == 2
    json.loads(lines[0])
    json.loads(lines[1])


def test_extract_jsonl_events_no_valid_lines(tmp_path):
    """_extract_jsonl_events writes an empty file when there are no valid lines.

    v0.7.4: The method now streams to disk (always creates the file),
    rather than accumulating lines in a list and only writing if non-empty.
    """
    stdout = "not json\n"
    output_path = tmp_path / "events.jsonl"
    OpenHandsExecutor._extract_jsonl_events(stdout, output_path)
    # The file is created but should be empty (no valid JSONL lines).
    assert output_path.is_file()
    assert output_path.read_text() == ""


# --------------------------------------------------------------------------- #
# 5. OpenHandsInfo — metadata record
# --------------------------------------------------------------------------- #


def test_openhands_info_defaults():
    """OpenHandsInfo has correct defaults."""
    info = OpenHandsInfo()
    assert info.backend == "openhands"
    assert info.headless is True
    assert info.json_output is True
    assert info.runtime == "docker"


def test_openhands_info_to_dict():
    """OpenHandsInfo.to_dict returns a serializable dict."""
    info = OpenHandsInfo(
        openhands_version="0.1.0",
        model="claude-sonnet-4-20250514",
        events_captured=42,
    )
    d = info.to_dict()
    assert d["backend"] == "openhands"
    assert d["openhands_version"] == "0.1.0"
    assert d["model"] == "claude-sonnet-4-20250514"
    assert d["events_captured"] == 42


def test_executor_info_method():
    """OpenHandsExecutor.info() returns OpenHandsInfo with config values."""
    cfg = ExecutorSection(backend="openhands", agent="test-model")
    executor = OpenHandsExecutor(cfg)
    executor._version = "0.1.0"
    executor._events_captured = 10
    executor._working_dir = "/tmp/repo"

    info = executor.info()
    assert info.openhands_version == "0.1.0"
    assert info.model == "test-model"
    assert info.events_captured == 10
    assert info.working_dir == "/tmp/repo"


# --------------------------------------------------------------------------- #
# 6. Config — backend="openhands"
# --------------------------------------------------------------------------- #


def test_config_accepts_openhands_backend():
    """ExecutorSection accepts backend='openhands'."""
    cfg = ExecutorSection(backend="openhands", agent="claude")
    assert cfg.backend == "openhands"


def test_config_rejects_unknown_backend():
    """ExecutorSection rejects unknown backends."""
    with pytest.raises(ValueError, match="not valid"):
        ExecutorSection(backend="kubernetes")


def test_config_openhands_in_yaml(tmp_path):
    """Repo config loads openhands backend from YAML."""
    import yaml

    config_file = tmp_path / "test.repo.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "repo": {"name": "test", "path": str(tmp_path)},
                "executor": {
                    "backend": "openhands",
                    "agent": "claude-sonnet-4-20250514",
                    "network_policy": "locked_down",
                },
            }
        )
    )
    from acp.config import load_repo_config

    cfg = load_repo_config(config_file)
    assert cfg.executor.backend == "openhands"
    assert cfg.executor.agent == "claude-sonnet-4-20250514"


# --------------------------------------------------------------------------- #
# 7. fetch_remote and cleanup — interface compatibility
# --------------------------------------------------------------------------- #


def test_fetch_remote_returns_empty():
    """fetch_remote returns empty string (no remote for OpenHands)."""
    cfg = ExecutorSection(backend="openhands", agent="test")
    executor = OpenHandsExecutor(cfg)
    result = executor.fetch_remote(Path("/tmp"))
    assert result == ""


def test_cleanup_is_noop():
    """cleanup() is a no-op for OpenHands."""
    cfg = ExecutorSection(backend="openhands", agent="test")
    executor = OpenHandsExecutor(cfg)
    executor.cleanup()  # should not raise


def test_stop_returns_true():
    """stop() returns True for OpenHands (no persistent sandbox)."""
    cfg = ExecutorSection(backend="openhands", agent="test")
    executor = OpenHandsExecutor(cfg)
    assert executor.stop() is True
