"""v0.7.2 Phase 1 — Hermetic Agent Isolation tests.

Tests the new hermetic isolation feature:

  1. EnvironmentSpec — defaults, isolation flag, hash parsing
  2. AgentFile environment parsing — YAML loading, validation
  3. verify_environment_hash — lockfile hash verification
  4. AgentRegistry.verify_environment — registry-level verification
  5. VenvExecutor — backend name, install check, validation, env info
  6. Config — ExecutorSection accepts backend="venv"
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from acp.agents.agent_file import (
    AgentFile,
    EnvironmentSpec,
    compute_file_hash,
    load_agent_file,
    validate_agent_file_data,
    verify_environment_hash,
)
from acp.agents.agent_registry import AgentRegistry
from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.executor.venv_executor import VenvExecutor, VenvNotInstalledError

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_agent_data(
    name: str = "test-agent",
    version: str = "1.0.0",
    role: str = "coder",
    command_template: str = "test-agent --prompt {prompt_path}",
    environment: dict | None = None,
) -> dict:
    data = {
        "name": name,
        "version": version,
        "role": role,
        "command_template": command_template,
        "capabilities": ["code_edit"],
        "timeout_seconds": 1800,
        "max_repair_attempts": 5,
        "sha256": "",
    }
    if environment is not None:
        data["environment"] = environment
    return data


def _write_agent_yaml(
    tmp_path: Path,
    name: str,
    data: dict,
) -> Path:
    """Write an agent file as .agent.yaml."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{name}.agent.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


# --------------------------------------------------------------------------- #
# 1. EnvironmentSpec
# --------------------------------------------------------------------------- #


def test_environment_spec_defaults():
    """Default EnvironmentSpec has manager='none', is_isolated is False."""
    spec = EnvironmentSpec()
    assert spec.manager == "none"
    assert spec.lockfile == ""
    assert spec.dependencies_hash == ""
    assert spec.python_version == ""
    assert spec.is_isolated is False


def test_environment_spec_isolated():
    """EnvironmentSpec(manager='uv') is_isolated is True."""
    spec = EnvironmentSpec(
        manager="uv",
        lockfile="uv.lock",
        dependencies_hash="sha256:abc123",
        python_version="3.12",
    )
    assert spec.manager == "uv"
    assert spec.is_isolated is True


def test_environment_spec_hash_algorithm():
    """'sha256:abc123' → algorithm='sha256'."""
    spec = EnvironmentSpec(dependencies_hash="sha256:abc123")
    assert spec.hash_algorithm == "sha256"


def test_environment_spec_hash_value():
    """'sha256:abc123' → value='abc123'."""
    spec = EnvironmentSpec(dependencies_hash="sha256:abc123")
    assert spec.hash_value == "abc123"


def test_environment_spec_no_prefix_hash():
    """'abc123' (no prefix) → algorithm='sha256', value='abc123'."""
    spec = EnvironmentSpec(dependencies_hash="abc123")
    assert spec.hash_algorithm == "sha256"
    assert spec.hash_value == "abc123"


# --------------------------------------------------------------------------- #
# 2. AgentFile environment parsing
# --------------------------------------------------------------------------- #


def test_agent_file_no_environment(tmp_path):
    """Agent YAML without environment block → environment is None."""
    path = _write_agent_yaml(tmp_path, "test-agent", _make_agent_data())
    agent = load_agent_file(path)
    assert agent.environment is None


def test_agent_file_environment_none(tmp_path):
    """Environment block with manager='none' → environment is not None, is_isolated is False."""
    data = _make_agent_data(environment={"manager": "none"})
    path = _write_agent_yaml(tmp_path, "test-agent", data)
    agent = load_agent_file(path)
    assert agent.environment is not None
    assert agent.environment.manager == "none"
    assert agent.environment.is_isolated is False


def test_agent_file_environment_uv(tmp_path):
    """Environment block with manager='uv', lockfile, hash → is_isolated is True."""
    data = _make_agent_data(
        environment={
            "manager": "uv",
            "lockfile": "uv.lock",
            "dependencies_hash": "sha256:d3b07384d113edec49eaa6238ad5ff00",
            "python_version": "3.12",
        }
    )
    path = _write_agent_yaml(tmp_path, "test-agent", data)
    agent = load_agent_file(path)
    assert agent.environment is not None
    assert agent.environment.manager == "uv"
    assert agent.environment.lockfile == "uv.lock"
    assert agent.environment.dependencies_hash == "sha256:d3b07384d113edec49eaa6238ad5ff00"
    assert agent.environment.python_version == "3.12"
    assert agent.environment.is_isolated is True


def test_agent_file_environment_invalid_manager(tmp_path):
    """manager='cargo' → AgentConfigError."""
    data = _make_agent_data(environment={"manager": "cargo"})
    with pytest.raises(AgentConfigError, match="invalid"):
        validate_agent_file_data(data)


def test_agent_file_environment_uv_missing_lockfile(tmp_path):
    """manager='uv' but no lockfile → AgentConfigError."""
    data = _make_agent_data(
        environment={
            "manager": "uv",
            "dependencies_hash": "sha256:abc123",
        }
    )
    with pytest.raises(AgentConfigError, match="lockfile is required"):
        validate_agent_file_data(data)


def test_agent_file_environment_uv_missing_hash(tmp_path):
    """manager='uv' but no dependencies_hash → AgentConfigError."""
    data = _make_agent_data(
        environment={
            "manager": "uv",
            "lockfile": "uv.lock",
        }
    )
    with pytest.raises(AgentConfigError, match="dependencies_hash is required"):
        validate_agent_file_data(data)


def test_agent_file_environment_non_dict(tmp_path):
    """environment: 'bad' (non-dict) → AgentConfigError."""
    data = _make_agent_data()
    data["environment"] = "bad"
    with pytest.raises(AgentConfigError, match="must be a YAML mapping"):
        validate_agent_file_data(data)


def test_agent_file_to_dict_with_environment():
    """to_dict() includes environment block when isolated."""
    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(
            manager="uv",
            lockfile="uv.lock",
            dependencies_hash="sha256:abc123",
            python_version="3.12",
        ),
    )
    d = agent.to_dict()
    assert "environment" in d
    assert d["environment"]["manager"] == "uv"
    assert d["environment"]["lockfile"] == "uv.lock"
    assert d["environment"]["dependencies_hash"] == "sha256:abc123"
    assert d["environment"]["python_version"] == "3.12"


def test_agent_file_to_dict_without_environment():
    """to_dict() doesn't include environment when not isolated."""
    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(manager="none"),
    )
    d = agent.to_dict()
    assert "environment" not in d


# --------------------------------------------------------------------------- #
# 3. verify_environment_hash
# --------------------------------------------------------------------------- #


def test_verify_environment_hash_no_env():
    """Agent without environment → returns True."""
    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
    )
    assert verify_environment_hash(agent, Path("/tmp")) is True


def test_verify_environment_hash_not_isolated():
    """Agent with manager='none' → returns True."""
    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(manager="none"),
    )
    assert verify_environment_hash(agent, Path("/tmp")) is True


def test_verify_environment_hash_match(tmp_path):
    """Create a lockfile, compute its hash, set it in EnvironmentSpec, verify → True."""
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("dependencies: []\n")
    actual_hash = compute_file_hash(lockfile)

    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(
            manager="uv",
            lockfile="uv.lock",
            dependencies_hash=f"sha256:{actual_hash}",
        ),
    )
    assert verify_environment_hash(agent, tmp_path) is True


def test_verify_environment_hash_mismatch(tmp_path):
    """Wrong hash → AgentConfigError."""
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("dependencies: []\n")

    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(
            manager="uv",
            lockfile="uv.lock",
            dependencies_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        ),
    )
    with pytest.raises(AgentConfigError, match="dependency hash mismatch"):
        verify_environment_hash(agent, tmp_path)


def test_verify_environment_hash_missing_lockfile(tmp_path):
    """Lockfile doesn't exist → FileNotFoundError."""
    agent = AgentFile(
        name="test",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(
            manager="uv",
            lockfile="uv.lock",
            dependencies_hash="sha256:abc123",
        ),
    )
    with pytest.raises(FileNotFoundError, match="lockfile not found"):
        verify_environment_hash(agent, tmp_path)


# --------------------------------------------------------------------------- #
# 4. AgentRegistry environment verification
# --------------------------------------------------------------------------- #


def test_registry_verify_environment(tmp_path):
    """Register an agent with environment, verify_environment returns True."""
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("dependencies: []\n")
    actual_hash = compute_file_hash(lockfile)

    agent = AgentFile(
        name="isolated-agent",
        version="1.0.0",
        role="coder",
        command_template="cmd",
        environment=EnvironmentSpec(
            manager="uv",
            lockfile="uv.lock",
            dependencies_hash=f"sha256:{actual_hash}",
        ),
    )
    registry = AgentRegistry()
    registry.register(agent)
    assert registry.verify_environment(agent, tmp_path) is True


# --------------------------------------------------------------------------- #
# 5. VenvExecutor
# --------------------------------------------------------------------------- #


def test_venv_check_installed_no_uv():
    """Mock shutil.which to return None → check_installed() is False."""
    with patch("acp.executor.venv_executor.shutil.which", return_value=None):
        assert VenvExecutor.check_installed() is False


def test_venv_backend_name():
    """backend_name property returns 'venv'."""
    config = ExecutorSection(backend="venv", agent="python -m my_agent")
    executor = VenvExecutor(config)
    assert executor.backend_name == "venv"


def test_venv_validate_no_agent():
    """Empty agent config → AgentConfigError."""
    config = ExecutorSection(backend="venv", agent="")
    executor = VenvExecutor(config)
    with patch.object(VenvExecutor, "check_installed", return_value=True):
        with pytest.raises(AgentConfigError, match="executor.agent is required"):
            executor._validate()


def test_venv_validate_not_installed():
    """Mock check_installed False → VenvNotInstalledError."""
    config = ExecutorSection(backend="venv", agent="python -m my_agent")
    executor = VenvExecutor(config)
    with patch.object(VenvExecutor, "check_installed", return_value=False):
        with pytest.raises(VenvNotInstalledError, match="requires uv"):
            executor._validate()


def test_venv_get_environment_info():
    """get_environment_info returns dict with 'backend': 'venv'."""
    config = ExecutorSection(backend="venv", agent="python -m my_agent")
    executor = VenvExecutor(config)
    with patch.object(VenvExecutor, "get_version", return_value="uv 0.4.0"):
        info = executor.get_environment_info()
    assert info["backend"] == "venv"
    assert info["uv_version"] == "uv 0.4.0"


def test_venv_stop_no_process():
    """stop() returns True when no process is running."""
    config = ExecutorSection(backend="venv", agent="python -m my_agent")
    executor = VenvExecutor(config)
    assert executor.stop() is True


def test_venv_stop_terminated_process():
    """stop() returns True when the process has already exited."""
    config = ExecutorSection(backend="venv", agent="python -m my_agent")
    executor = VenvExecutor(config)
    # Simulate a completed process.
    mock_proc = Mock()
    mock_proc.poll.return_value = 0  # process has exited
    executor._proc = mock_proc
    assert executor.stop() is True


def test_venv_stop_kills_runaway_process():
    """stop() sends SIGTERM then SIGKILL to a running process."""
    import subprocess as sp

    # Start a real long-running subprocess.
    proc = sp.Popen(
        ["sleep", "30"],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
        text=True,
        start_new_session=True,
    )
    config = ExecutorSection(backend="venv", agent="python -m my_agent")
    executor = VenvExecutor(config)
    executor._proc = proc
    assert executor.stop() is True
    assert proc.poll() is not None  # process is dead


# --------------------------------------------------------------------------- #
# 6. Config validation
# --------------------------------------------------------------------------- #


def test_config_executor_venv_backend():
    """ExecutorSection(backend='venv') is valid."""
    config = ExecutorSection(backend="venv")
    assert config.backend == "venv"
