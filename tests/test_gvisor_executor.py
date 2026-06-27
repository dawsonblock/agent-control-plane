"""Unit tests for acp.executor.gvisor — validation logic (no Docker needed)."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.executor.gvisor import GvisorExecutor, GvisorNotInstalledError


def _gvisor_config(**overrides: object) -> ExecutorSection:
    defaults: dict[str, object] = {
        "backend": "gvisor",
        "agent": "claude",
        "clone_mode": True,
        "network_policy": "locked_down",
    }
    defaults.update(overrides)
    return ExecutorSection(**defaults)


# --- check_installed / get_version ------------------------------------------ #


def test_gvisor_check_installed_no_docker() -> None:
    with patch("acp.executor.gvisor.shutil.which", return_value=None):
        assert GvisorExecutor.check_installed() is False


def test_gvisor_get_version_not_installed() -> None:
    def _raise(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError("runsc not found")

    with patch("acp.executor.gvisor.subprocess.run", side_effect=_raise):
        assert GvisorExecutor.get_version() == ""


# --- backend_name ----------------------------------------------------------- #


def test_gvisor_backend_name() -> None:
    executor = GvisorExecutor(_gvisor_config())
    assert executor.backend_name == "gvisor"


# --- _validate -------------------------------------------------------------- #


def test_gvisor_validate_not_installed() -> None:
    executor = GvisorExecutor(_gvisor_config())
    with patch.object(GvisorExecutor, "check_installed", return_value=False):
        with pytest.raises(GvisorNotInstalledError):
            executor._validate()


def test_gvisor_validate_clone_mode_false() -> None:
    executor = GvisorExecutor(_gvisor_config(clone_mode=False))
    with patch.object(GvisorExecutor, "check_installed", return_value=True):
        with pytest.raises(AgentConfigError, match="clone_mode"):
            executor._validate()


def test_gvisor_validate_network_open() -> None:
    executor = GvisorExecutor(_gvisor_config(network_policy="open"))
    with patch.object(GvisorExecutor, "check_installed", return_value=True):
        with pytest.raises(AgentConfigError, match="network_policy"):
            executor._validate()


def test_gvisor_validate_no_agent() -> None:
    executor = GvisorExecutor(_gvisor_config(agent=""))
    with patch.object(GvisorExecutor, "check_installed", return_value=True):
        with pytest.raises(AgentConfigError, match="agent is required"):
            executor._validate()


def test_gvisor_validate_passes() -> None:
    executor = GvisorExecutor(_gvisor_config())
    with patch.object(GvisorExecutor, "check_installed", return_value=True):
        executor._validate()  # should not raise
