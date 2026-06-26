"""M9 Agent File registry tests.

Tests the agent file registry feature:

  1. AgentFile — loading, validation, schema
  2. Hash verification — compute_file_hash, verify_agent_hash
  3. AgentRegistry — load from dir, lookup, verify
  4. build_agent integration — hash mismatch refusal
  5. Config — agents_dir parsing
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from acp.agents.agent_file import (
    AgentFile,
    compute_file_hash,
    load_agent_file,
    validate_agent_file_data,
    verify_agent_hash,
)
from acp.agents.agent_registry import AgentRegistry, load_registry
from acp.errors import AgentConfigError


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_agent_data(
    name: str = "test-agent",
    version: str = "1.0.0",
    role: str = "coder",
    command_template: str = "test-agent --prompt {prompt_path}",
    capabilities: list[str] | None = None,
    sha256: str = "",
    binary_path: str | None = None,
) -> dict:
    data = {
        "name": name,
        "version": version,
        "role": role,
        "command_template": command_template,
        "capabilities": capabilities or ["code_edit"],
        "timeout_seconds": 1800,
        "max_repair_attempts": 5,
        "sha256": sha256,
    }
    if binary_path is not None:
        data["binary_path"] = binary_path
    return data


def _write_agent_yaml(
    agents_dir: Path,
    name: str,
    data: dict,
) -> Path:
    """Write an agent file as .agent.yaml."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.agent.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


# --------------------------------------------------------------------------- #
# 1. AgentFile loading and validation
# --------------------------------------------------------------------------- #


class TestAgentFileLoading:
    """load_agent_file — load from YAML."""

    def test_load_valid_agent(self, tmp_path):
        path = _write_agent_yaml(tmp_path, "test-agent", _make_agent_data())
        agent = load_agent_file(path)

        assert agent.name == "test-agent"
        assert agent.version == "1.0.0"
        assert agent.role == "coder"
        assert agent.command_template == "test-agent --prompt {prompt_path}"
        assert agent.capabilities == ["code_edit"]
        assert agent.source_path == path

    def test_load_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_agent_file(tmp_path / "nonexistent.agent.yaml")

    def test_load_malformed_yaml(self, tmp_path):
        path = tmp_path / "bad.agent.yaml"
        path.write_text("name: [unclosed")
        with pytest.raises(AgentConfigError):
            load_agent_file(path)

    def test_load_not_a_mapping(self, tmp_path):
        path = tmp_path / "list.agent.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(AgentConfigError):
            load_agent_file(path)


class TestAgentFileValidation:
    """validate_agent_file_data — schema validation."""

    def test_valid_data(self):
        agent = validate_agent_file_data(_make_agent_data())
        assert agent.name == "test-agent"

    def test_missing_name(self):
        data = _make_agent_data()
        del data["name"]
        with pytest.raises(AgentConfigError, match="missing required field 'name'"):
            validate_agent_file_data(data)

    def test_missing_version(self):
        data = _make_agent_data()
        del data["version"]
        with pytest.raises(AgentConfigError, match="missing required field 'version'"):
            validate_agent_file_data(data)

    def test_missing_role(self):
        data = _make_agent_data()
        del data["role"]
        with pytest.raises(AgentConfigError, match="missing required field 'role'"):
            validate_agent_file_data(data)

    def test_missing_command_template(self):
        data = _make_agent_data()
        del data["command_template"]
        with pytest.raises(AgentConfigError, match="missing required field 'command_template'"):
            validate_agent_file_data(data)

    def test_invalid_role(self):
        data = _make_agent_data(role="invalid_role")
        with pytest.raises(AgentConfigError, match="invalid"):
            validate_agent_file_data(data)

    def test_empty_name(self):
        data = _make_agent_data(name="")
        with pytest.raises(AgentConfigError, match="non-empty string"):
            validate_agent_file_data(data)

    def test_empty_command_template(self):
        data = _make_agent_data(command_template="")
        with pytest.raises(AgentConfigError, match="non-empty string"):
            validate_agent_file_data(data)

    def test_invalid_capabilities(self):
        data = _make_agent_data()
        data["capabilities"] = "not a list"
        with pytest.raises(AgentConfigError, match="capabilities.*list"):
            validate_agent_file_data(data)

    def test_invalid_timeout(self):
        data = _make_agent_data()
        data["timeout_seconds"] = -1
        with pytest.raises(AgentConfigError, match="positive int"):
            validate_agent_file_data(data)

    def test_invalid_max_repair(self):
        data = _make_agent_data()
        data["max_repair_attempts"] = -1
        with pytest.raises(AgentConfigError, match="non-negative int"):
            validate_agent_file_data(data)


class TestAgentFileModel:
    """AgentFile — model behavior."""

    def test_repr(self):
        agent = AgentFile(
            name="test",
            version="1.0.0",
            role="coder",
            command_template="test",
        )
        assert "AgentFile" in repr(agent)
        assert "test" in repr(agent)

    def test_equality(self):
        a1 = AgentFile(name="test", version="1.0", role="coder", command_template="cmd")
        a2 = AgentFile(name="test", version="1.0", role="coder", command_template="cmd")
        a3 = AgentFile(name="other", version="1.0", role="coder", command_template="cmd")
        assert a1 == a2
        assert a1 != a3

    def test_to_dict(self):
        agent = AgentFile(
            name="test",
            version="1.0.0",
            role="coder",
            command_template="cmd",
            capabilities=["code_edit"],
        )
        d = agent.to_dict()
        assert d["name"] == "test"
        assert d["role"] == "coder"
        assert d["capabilities"] == ["code_edit"]

    def test_invalid_role_in_constructor(self):
        with pytest.raises(AgentConfigError, match="invalid"):
            AgentFile(name="t", version="1", role="bad", command_template="c")


# --------------------------------------------------------------------------- #
# 2. Hash verification
# --------------------------------------------------------------------------- #


class TestHashVerification:
    """compute_file_hash and verify_agent_hash."""

    def test_compute_file_hash(self, tmp_path):
        path = tmp_path / "binary"
        path.write_bytes(b"hello world")
        h = compute_file_hash(path)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest

    def test_compute_file_hash_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            compute_file_hash(tmp_path / "nonexistent")

    def test_compute_file_hash_deterministic(self, tmp_path):
        path = tmp_path / "binary"
        path.write_bytes(b"same content")
        h1 = compute_file_hash(path)
        h2 = compute_file_hash(path)
        assert h1 == h2

    def test_verify_agent_hash_no_binary(self):
        """No binary_path → verification skipped (returns True)."""
        agent = AgentFile(
            name="test", version="1", role="coder",
            command_template="c", sha256="abc",
        )
        assert verify_agent_hash(agent) is True

    def test_verify_agent_hash_no_hash(self):
        """No sha256 → verification skipped."""
        agent = AgentFile(
            name="test", version="1", role="coder",
            command_template="c", binary_path=Path("/bin/ls"),
        )
        assert verify_agent_hash(agent) is True

    def test_verify_agent_hash_matches(self, tmp_path):
        binary = tmp_path / "agent"
        binary.write_bytes(b"agent binary content")
        h = compute_file_hash(binary)

        agent = AgentFile(
            name="test", version="1", role="coder",
            command_template="c", sha256=h, binary_path=binary,
        )
        assert verify_agent_hash(agent) is True

    def test_verify_agent_hash_mismatch(self, tmp_path):
        binary = tmp_path / "agent"
        binary.write_bytes(b"agent binary content")

        agent = AgentFile(
            name="test", version="1", role="coder",
            command_template="c",
            sha256="0" * 64,  # wrong hash
            binary_path=binary,
        )
        with pytest.raises(AgentConfigError, match="hash mismatch"):
            verify_agent_hash(agent)

    def test_verify_agent_hash_binary_not_found(self, tmp_path):
        agent = AgentFile(
            name="test", version="1", role="coder",
            command_template="c",
            sha256="abc",
            binary_path=tmp_path / "nonexistent",
        )
        with pytest.raises(FileNotFoundError):
            verify_agent_hash(agent)


# --------------------------------------------------------------------------- #
# 3. AgentRegistry
# --------------------------------------------------------------------------- #


class TestAgentRegistry:
    """AgentRegistry — load, lookup, verify."""

    def test_load_from_dir(self, tmp_path):
        _write_agent_yaml(tmp_path, "agent-a", _make_agent_data("agent-a"))
        _write_agent_yaml(tmp_path, "agent-b", _make_agent_data("agent-b"))

        registry = AgentRegistry(tmp_path)
        assert len(registry) == 2
        assert "agent-a" in registry
        assert "agent-b" in registry
        assert registry.has("agent-a")
        assert not registry.has("nonexistent")

    def test_empty_dir(self, tmp_path):
        registry = AgentRegistry(tmp_path)
        assert len(registry) == 0
        assert registry.list_agents() == []

    def test_nonexistent_dir(self):
        registry = AgentRegistry(Path("/nonexistent"))
        assert len(registry) == 0

    def test_none_dir(self):
        registry = AgentRegistry(None)
        assert len(registry) == 0

    def test_get(self, tmp_path):
        _write_agent_yaml(tmp_path, "test-agent", _make_agent_data("test-agent"))
        registry = AgentRegistry(tmp_path)

        agent = registry.get("test-agent")
        assert agent is not None
        assert agent.name == "test-agent"

    def test_get_not_found(self, tmp_path):
        registry = AgentRegistry(tmp_path)
        assert registry.get("nonexistent") is None

    def test_list_agents_sorted(self, tmp_path):
        _write_agent_yaml(tmp_path, "zebra", _make_agent_data("zebra"))
        _write_agent_yaml(tmp_path, "alpha", _make_agent_data("alpha"))

        registry = AgentRegistry(tmp_path)
        assert registry.list_agents() == ["alpha", "zebra"]

    def test_load_errors_recorded(self, tmp_path):
        """Malformed agent files are recorded in load_errors, not raised."""
        _write_agent_yaml(tmp_path, "good", _make_agent_data("good"))
        # Write a bad file
        (tmp_path / "bad.agent.yaml").write_text("name: [unclosed")

        registry = AgentRegistry(tmp_path)
        assert "good" in registry
        assert len(registry.load_errors) > 0

    def test_register_manual(self):
        registry = AgentRegistry(None)
        agent = AgentFile(name="manual", version="1", role="coder", command_template="c")
        registry.register(agent)
        assert registry.has("manual")

    def test_register_duplicate_raises(self):
        registry = AgentRegistry(None)
        agent = AgentFile(name="dup", version="1", role="coder", command_template="c")
        registry.register(agent)
        with pytest.raises(AgentConfigError, match="already registered"):
            registry.register(agent)

    def test_verify_by_name(self, tmp_path):
        binary = tmp_path / "agent_bin"
        binary.write_bytes(b"binary content")
        h = compute_file_hash(binary)

        agents_dir = tmp_path / "agents"
        _write_agent_yaml(agents_dir, "verified", _make_agent_data(
            "verified", sha256=h, binary_path=str(binary),
        ))

        registry = AgentRegistry(agents_dir)
        assert registry.verify_by_name("verified") is True

    def test_verify_by_name_not_registered(self):
        registry = AgentRegistry(None)
        with pytest.raises(KeyError, match="not registered"):
            registry.verify_by_name("nonexistent")

    def test_load_registry_convenience(self, tmp_path):
        _write_agent_yaml(tmp_path, "conv", _make_agent_data("conv"))
        registry = load_registry(tmp_path)
        assert registry.has("conv")


# --------------------------------------------------------------------------- #
# 4. build_agent integration
# --------------------------------------------------------------------------- #


class TestBuildAgentIntegration:
    """build_agent — hash verification integration."""

    def test_build_agent_shell_no_registry(self, tmp_path):
        """Shell agent works without a registry."""
        from acp.agents.registry import build_agent
        from acp.config import RepoConfig, RepoSection
        from acp.agents.shell_agent import ShellAgent

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )
        agent = build_agent(cfg)
        assert isinstance(agent, ShellAgent)

    def test_build_agent_with_registry_agent_not_found(self, tmp_path):
        """Agent not in registry — allowed (could be built-in)."""
        from acp.agents.registry import build_agent
        from acp.config import RepoConfig, RepoSection
        from acp.agents.shell_agent import ShellAgent

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # No agent files — registry is empty

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )
        cfg.agent.agents_dir = agents_dir
        agent = build_agent(cfg)
        assert isinstance(agent, ShellAgent)

    def test_build_agent_hash_mismatch_refuses(self, tmp_path):
        """Hash mismatch raises AgentConfigError."""
        from acp.agents.registry import build_agent
        from acp.config import RepoConfig, RepoSection

        binary = tmp_path / "agent_bin"
        binary.write_bytes(b"real binary content")

        agents_dir = tmp_path / "agents"
        _write_agent_yaml(agents_dir, "shell", _make_agent_data(
            "shell", sha256="0" * 64, binary_path=str(binary),
        ))

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )
        cfg.agent.agents_dir = agents_dir

        with pytest.raises(AgentConfigError, match="hash mismatch"):
            build_agent(cfg)

    def test_build_agent_hash_match_allowed(self, tmp_path):
        """Hash match — agent is allowed to run."""
        from acp.agents.registry import build_agent
        from acp.config import RepoConfig, RepoSection
        from acp.agents.shell_agent import ShellAgent

        binary = tmp_path / "agent_bin"
        binary.write_bytes(b"binary content")
        h = compute_file_hash(binary)

        agents_dir = tmp_path / "agents"
        _write_agent_yaml(agents_dir, "shell", _make_agent_data(
            "shell", sha256=h, binary_path=str(binary),
        ))

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )
        cfg.agent.agents_dir = agents_dir

        agent = build_agent(cfg)
        assert isinstance(agent, ShellAgent)


# --------------------------------------------------------------------------- #
# 5. Config parsing
# --------------------------------------------------------------------------- #


class TestAgentConfig:
    """AgentSection agents_dir parsing."""

    def test_default_no_agents_dir(self):
        from acp.config import AgentSection
        a = AgentSection()
        assert a.agents_dir is None

    def test_agents_dir_set(self, tmp_path):
        from acp.config import AgentSection
        a = AgentSection(agents_dir=tmp_path / "agents")
        assert a.agents_dir is not None
        assert a.agents_dir.is_absolute()

    def test_repo_config_with_agents_dir(self, tmp_path):
        from acp.config import AgentSection, RepoConfig, RepoSection
        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            agent=AgentSection(agents_dir=tmp_path / "agents"),
        )
        assert cfg.agent.agents_dir is not None
