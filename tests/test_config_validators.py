"""Tests for src/acp/config.py — repo config loading and section validators.

Covers ``load_repo_config`` and the field/model validators on each section
(``AgentSection``, ``ReviewSection``, ``ExecutorSection``, etc.). Configs
are written as YAML files in ``tmp_path`` and loaded through the public
``load_repo_config`` entry point so the full parse → validate path is
exercised end to end.
"""

from __future__ import annotations

import re

import pytest
import yaml
from pydantic import ValidationError

from acp.config import (
    AgentSection,
    DurableMode,
    EvidenceSection,
    ExecutorSection,
    FederationSection,
    FederationServerConfig,
    RepoConfig,
    ReviewSection,
    load_repo_config,
)
from acp.models import RiskLevel

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write_config(tmp_path, data: dict) -> str:
    """Write a YAML config dict to tmp_path/test.repo.yaml and return the path."""
    config_file = tmp_path / "test.repo.yaml"
    config_file.write_text(yaml.dump(data))
    return str(config_file)


def _minimal_repo(tmp_path) -> dict:
    return {"repo": {"name": "test", "path": str(tmp_path)}}


# --------------------------------------------------------------------------- #
# load_repo_config — top-level loading
# --------------------------------------------------------------------------- #


def test_load_config_minimal(tmp_path):
    """A minimal valid config with just the repo section loads."""
    path = _write_config(tmp_path, _minimal_repo(tmp_path))
    cfg = load_repo_config(path)
    assert isinstance(cfg, RepoConfig)
    assert cfg.repo.name == "test"
    assert cfg.repo.path == tmp_path.resolve()
    # Defaults are populated for the optional sections.
    assert cfg.agent.default == "shell"
    assert cfg.review.require_human_approval is True
    assert cfg.source_path is not None


def test_load_config_full(tmp_path):
    """A config with every section populated loads and validates."""
    data = {
        "repo": {"name": "full", "path": str(tmp_path), "default_branch": "develop"},
        "agent": {
            "default": "custom",
            "timeout_seconds": 600,
            "max_repair_attempts": 3,
            "max_subtasks": 8,
            "command_template": "echo {request}",
        },
        "commands": {
            "install": "pip install -e .",
            "lint": "ruff check .",
            "typecheck": "mypy .",
            "test": "pytest",
            "build": "python -m build",
            "timeout_seconds": 300,
        },
        "review": {
            "max_changed_files": 50,
            "max_added_lines": 5000,
            "block_secret_leaks": True,
            "require_human_approval": True,
        },
        "context": {"include": ["src/**"], "exclude": ["**/.venv/**"]},
        "memory": {"graphiti_group_id": "grp", "promote_reports_by_default": True},
        "evidence": {"durable_mode": "best_effort", "task_store_primary": "json"},
        "executor": {"backend": "docker_sbx", "network_policy": "balanced"},
        "skills": {"active_skill": "refactor"},
        "federation": {
            "servers": [
                {"name": "local", "transport": "stdio", "command": ["echo", "hi"]},
            ]
        },
        "mission": {"missions_dir": str(tmp_path / "missions")},
        "proxy": {"enabled": True, "proxy_port": 9090, "allowed_domains": ["example.com"]},
        "reranking": {"enabled": True, "top_k_before_rerank": 10, "top_k_after_rerank": 3},
    }
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert cfg.repo.default_branch == "develop"
    assert cfg.agent.default == "custom"
    assert cfg.agent.max_subtasks == 8
    assert cfg.commands.test == "pytest"
    assert cfg.executor.backend == "docker_sbx"
    assert cfg.executor.network_policy == "balanced"
    assert cfg.federation.servers[0].name == "local"
    assert cfg.proxy.proxy_port == 9090
    assert cfg.reranking.enabled is True


def test_load_config_missing_repo(tmp_path):
    """A config without a repo section raises a validation error."""
    path = _write_config(tmp_path, {"agent": {"default": "shell"}})
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_missing_file(tmp_path):
    """Loading a non-existent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_repo_config(tmp_path / "nope.repo.yaml")


def test_load_config_invalid_yaml(tmp_path):
    """Malformed YAML raises a ValueError (yaml.safe_load error)."""
    config_file = tmp_path / "bad.repo.yaml"
    config_file.write_text("repo: [unterminated\n  - oops: : :")
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_repo_config(config_file)


def test_load_config_none_sections_use_defaults(tmp_path):
    """Explicit ``None`` sections are dropped so defaults apply."""
    data = _minimal_repo(tmp_path)
    data["review"] = None
    data["skills"] = None
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert cfg.review.require_human_approval is True
    assert cfg.skills.active_skill == ""


# --------------------------------------------------------------------------- #
# AgentSection validators
# --------------------------------------------------------------------------- #


def test_load_config_agent_default(tmp_path):
    """agent.default is normalized to lowercase and validated."""
    data = _minimal_repo(tmp_path)
    data["agent"] = {"default": "Shell"}
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert cfg.agent.default == "shell"

    # Invalid agent kind is rejected.
    data["agent"] = {"default": "unknown-agent"}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_timeout_range(tmp_path):
    """timeout_seconds out of the [1, 86400] range raises (not clamped)."""
    data = _minimal_repo(tmp_path)
    data["agent"] = {"timeout_seconds": 0}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)

    data["agent"] = {"timeout_seconds": 86401}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)

    # Boundary values are accepted.
    for ok in (1, 86400):
        data["agent"] = {"timeout_seconds": ok}
        path = _write_config(tmp_path, data)
        cfg = load_repo_config(path)
        assert cfg.agent.timeout_seconds == ok


def test_load_config_max_repair_attempts_range(tmp_path):
    """max_repair_attempts must be in [0, 20]."""
    data = _minimal_repo(tmp_path)
    data["agent"] = {"max_repair_attempts": 21}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)

    data["agent"] = {"max_repair_attempts": 0}
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert cfg.agent.max_repair_attempts == 0


def test_load_config_max_subtasks_range(tmp_path):
    """max_subtasks must be in [0, 100]."""
    data = _minimal_repo(tmp_path)
    data["agent"] = {"max_subtasks": 101}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


# --------------------------------------------------------------------------- #
# ExecutorSection validators
# --------------------------------------------------------------------------- #


def test_load_config_executor_backend_valid(tmp_path):
    """Valid executor.backend values are accepted."""
    for backend in ("worktree", "docker_sbx", "gvisor", "openhands"):
        data = _minimal_repo(tmp_path)
        data["executor"] = {"backend": backend}
        path = _write_config(tmp_path, data)
        cfg = load_repo_config(path)
        assert cfg.executor.backend == backend


def test_load_config_executor_backend_invalid(tmp_path):
    """An invalid executor.backend value is rejected."""
    data = _minimal_repo(tmp_path)
    data["executor"] = {"backend": "cli"}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_network_policy_valid(tmp_path):
    """Valid network_policy values are accepted."""
    for policy in ("locked_down", "balanced", "open"):
        data = _minimal_repo(tmp_path)
        data["executor"] = {"network_policy": policy}
        path = _write_config(tmp_path, data)
        cfg = load_repo_config(path)
        assert cfg.executor.network_policy == policy


def test_load_config_network_policy_invalid(tmp_path):
    """An invalid network_policy value is rejected."""
    data = _minimal_repo(tmp_path)
    data["executor"] = {"network_policy": "wide_open"}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


# --------------------------------------------------------------------------- #
# ReviewSection validators
# --------------------------------------------------------------------------- #


def test_load_config_custom_secret_regexes(tmp_path):
    """Custom secret regex patterns are accepted and compiled at validation."""
    data = _minimal_repo(tmp_path)
    data["review"] = {
        "custom_secret_regexes": [
            {"name": "internal_api_key", "pattern": r"IAK-[A-Z0-9]{32}"},
            {"name": "legacy_token", "pattern": r"LT-\d+"},
        ]
    }
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert len(cfg.review.custom_secret_regexes) == 2
    assert cfg.review.custom_secret_regexes[0]["name"] == "internal_api_key"
    # The patterns are valid regex (validation would have raised otherwise).
    assert re.compile(cfg.review.custom_secret_regexes[0]["pattern"])


def test_load_config_invalid_regex(tmp_path):
    """An invalid regex pattern in custom_secret_regexes raises."""
    data = _minimal_repo(tmp_path)
    data["review"] = {
        "custom_secret_regexes": [
            {"name": "bad", "pattern": r"[unclosed"},
        ]
    }
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_custom_secret_regex_missing_keys(tmp_path):
    """A regex entry missing 'name' or 'pattern' is rejected."""
    data = _minimal_repo(tmp_path)
    data["review"] = {"custom_secret_regexes": [{"name": "no_pattern"}]}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_review_max_files_range(tmp_path):
    """max_changed_files must be in [1, 10000]."""
    data = _minimal_repo(tmp_path)
    data["review"] = {"max_changed_files": 0}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)

    data["review"] = {"max_changed_files": 10001}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_autonomous_mode(tmp_path):
    """review.autonomous_mode = True is accepted."""
    data = _minimal_repo(tmp_path)
    data["review"] = {"autonomous_mode": True, "auto_merge": True}
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert cfg.review.autonomous_mode is True
    assert cfg.review.auto_merge is True


def test_load_config_auto_merge_max_risk(tmp_path):
    """Valid risk levels are accepted for auto_merge_max_risk."""
    for risk in ("low", "medium", "high"):
        data = _minimal_repo(tmp_path)
        data["review"] = {"auto_merge_max_risk": risk}
        path = _write_config(tmp_path, data)
        cfg = load_repo_config(path)
        assert cfg.review.auto_merge_max_risk == RiskLevel(risk)

    # Invalid risk level is rejected.
    data = _minimal_repo(tmp_path)
    data["review"] = {"auto_merge_max_risk": "critical"}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


# --------------------------------------------------------------------------- #
# EvidenceSection + FederationServerConfig validators
# --------------------------------------------------------------------------- #


def test_load_config_sqlite_primary_requires_store(tmp_path):
    """task_store_primary='sqlite' requires durable_store to be set."""
    data = _minimal_repo(tmp_path)
    data["evidence"] = {"task_store_primary": "sqlite"}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)

    # With a durable_store it is accepted.
    data["evidence"] = {
        "task_store_primary": "sqlite",
        "durable_store": str(tmp_path / "store.db"),
    }
    path = _write_config(tmp_path, data)
    cfg = load_repo_config(path)
    assert cfg.evidence.task_store_primary == "sqlite"


def test_load_config_durable_mode_enum(tmp_path):
    """durable_mode accepts the valid enum string values."""
    for mode in ("disabled", "best_effort", "required"):
        data = _minimal_repo(tmp_path)
        data["evidence"] = {"durable_mode": mode}
        path = _write_config(tmp_path, data)
        cfg = load_repo_config(path)
        assert cfg.evidence.durable_mode == DurableMode(mode)


def test_load_config_federation_stdio_requires_command(tmp_path):
    """A stdio federation server without a command is rejected."""
    data = _minimal_repo(tmp_path)
    data["federation"] = {"servers": [{"name": "s", "transport": "stdio"}]}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


def test_load_config_federation_http_requires_url(tmp_path):
    """An http federation server without a url is rejected."""
    data = _minimal_repo(tmp_path)
    data["federation"] = {"servers": [{"name": "s", "transport": "http"}]}
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        load_repo_config(path)


# --------------------------------------------------------------------------- #
# Direct section construction (unit-level validator coverage)
# --------------------------------------------------------------------------- #


def test_agent_section_invalid_default():
    with pytest.raises(ValidationError):
        AgentSection(default="bogus")


def test_executor_section_invalid_backend():
    with pytest.raises(ValidationError):
        ExecutorSection(backend="bogus")


def test_executor_section_invalid_network_policy():
    with pytest.raises(ValidationError):
        ExecutorSection(network_policy="bogus")


def test_review_section_invalid_regex_entry():
    with pytest.raises(ValidationError):
        ReviewSection(custom_secret_regexes=[{"name": "x", "pattern": "("}])


def test_federation_server_config_valid():
    cfg = FederationServerConfig(name="s", transport="stdio", command=["echo"])
    assert cfg.transport == "stdio"
    assert cfg.timeout_seconds == 30


def test_federation_section_default_empty():
    cfg = FederationSection()
    assert cfg.servers == []


def test_evidence_section_default_primary_json():
    cfg = EvidenceSection()
    assert cfg.task_store_primary == "json"
