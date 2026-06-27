"""Venv executor — hermetic Python agent isolation via ``uv run --isolated``.

v0.7.2 (Phase 1 — Hermetic Agent Isolation): Runs Python-based agents in
an ephemeral, isolated virtual environment using ``uv run --isolated``.
This prevents supply-chain attacks via hijacked host-level Python
dependencies — even if the agent's entrypoint binary hash matches, a
modified ``uv.lock`` or ``requirements.txt`` would be caught by the
:func:`acp.agents.agent_file.verify_environment_hash` check, and the
isolated venv ensures the agent can't access host packages outside its
declared dependency tree.

This executor is lighter-weight than ``docker_sbx`` or ``gvisor`` (no
container runtime required) but stronger than bare ``worktree`` mode
(which provides no Python-level isolation). It's the recommended backend
for Python-based agents on Mac (the project's primary platform).

Configuration (ExecutorSection):
  - ``backend``: must be ``"venv"``
  - ``agent``: the agent command to run (e.g., ``"python -m my_agent"``)
  - ``lockfile``: path to the lockfile (relative to repo root)
  - ``dependencies_hash``: expected hash of the lockfile
  - ``python_version``: required Python version (e.g., ``"3.12"``)
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.models import AgentResult

logger = logging.getLogger(__name__)


class VenvNotInstalledError(Exception):
    """Raised when ``uv`` is not installed but backend='venv'."""


class VenvExecutor:
    """Runs Python agents in an isolated ``uv`` virtual environment.

    Implements the :class:`Executor` protocol. The agent runs via
    ``uv run --isolated`` which creates an ephemeral venv from the
    project's lockfile, completely detached from the host's global
    Python packages. This ensures the execution environment matches
    the exact cryptographic lockfile pinned in the agent's
    :class:`EnvironmentSpec`.
    """

    def __init__(self, config: ExecutorSection) -> None:
        self.config = config
        self._env_info: dict[str, str] = {
            "python_version": self._detect_python_version(),
            "isolated": "true",
        }

    @property
    def backend_name(self) -> str:
        return "venv"

    # ------------------------------------------------------------------ #
    # Pre-flight validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def check_installed() -> bool:
        """Return True if ``uv`` is available on PATH."""
        return shutil.which("uv") is not None

    @staticmethod
    def get_version() -> str:
        """Return the uv version string, or empty if not installed."""
        try:
            proc = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() or proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def _validate(self) -> None:
        """Fail-closed checks before starting a venv-isolated run."""
        if not self.check_installed():
            raise VenvNotInstalledError(
                "executor.backend='venv' requires uv to be installed. "
                "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
            )
        if not self.config.agent:
            raise AgentConfigError(
                "executor.agent is required when backend='venv'. "
                "Specify the Python command to run inside the isolated venv."
            )

    def get_environment_info(self) -> dict[str, str]:
        """Return environment metadata for the agent.started event payload.

        This records the locked environment state so the evidence trail
        proves the agent executed in the exact, untampered dependency tree
        specified in the registry.
        """
        return {
            "backend": "venv",
            "uv_version": self.get_version(),
            **self._env_info,
        }

    # ------------------------------------------------------------------ #
    # Run the agent
    # ------------------------------------------------------------------ #

    def start(
        self,
        *,
        task_id: str,
        prompt_path: Path,
        repo_path: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> AgentResult:
        """Run the agent in an isolated uv venv and return the result.

        The command is wrapped in ``uv run --isolated -- <agent command>``
        which creates an ephemeral venv from the repo's lockfile. The
        agent's stdout/stderr are captured to artifact files.
        """
        self._validate()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"

        # Build the uv run command.
        # --isolated: don't inherit the host's virtual environment
        # --no-project: don't load pyproject.toml from cwd (use the lockfile)
        # The agent command is appended after `--` so uv doesn't parse it.
        agent_command = self.config.agent
        cmd = [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--",
            *shlex.split(agent_command),
        ]

        logger.info(
            "venv: starting isolated agent for task %s (uv run --isolated)",
            task_id,
        )

        try:
            prompt_content = prompt_path.read_text()
            proc = subprocess.run(
                cmd,
                input=prompt_content,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            exit_code = 124
            out, err = "", f"venv: agent timed out after {timeout_seconds}s"
        except FileNotFoundError as exc:
            exit_code = 127
            out, err = "", f"venv: uv not found: {exc}"

        stdout_path.write_text(out)
        stderr_path.write_text(err)

        return AgentResult(
            agent_name=f"venv:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def _detect_python_version(self) -> str:
        """Detect the Python version that uv will use."""
        try:
            proc = subprocess.run(
                ["uv", "python", "find"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.stdout.strip():
                py_path = proc.stdout.strip()
                if not Path(py_path).is_file():
                    return "unknown"
                ver_proc = subprocess.run(
                    [py_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if ver_proc.returncode == 0:
                    return ver_proc.stdout.strip().replace("Python ", "")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    # ------------------------------------------------------------------ #
    # Cleanup (no-op — uv venvs are ephemeral)
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Stop the agent. No-op for venv (subprocess.run is blocking)."""
        pass

    def cleanup(self) -> None:
        """Clean up resources. No-op for venv (ephemeral venvs are GC'd by uv)."""
        pass

    def fetch_remote(self) -> str:
        """Fetch the agent's git remote. Not applicable for venv (no remote)."""
        return ""
