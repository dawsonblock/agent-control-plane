"""Docker Sandboxes (``sbx``) executor backend (v0.5.13).

Runs the coding agent inside an isolated microVM sandbox via Docker's
``sbx`` CLI. Each sandbox gets its own Docker daemon, filesystem, and
network. In clone mode (the only mode ACP allows), the sandbox keeps a
private Git clone inside the microVM and mounts the host repo read-only.
The sandbox exposes its clone as a ``sandbox-<name>`` remote on the host,
so ACP can fetch and diff the agent's work without the agent ever
touching the host filesystem.

Flow:
  1. ACP creates the task/run/evidence directory (host).
  2. SbxExecutor starts ``sbx run --clone --name <name> <agent>``.
  3. The agent works inside the microVM's private clone.
  4. ACP fetches the sandbox remote (``git fetch sandbox-<name>``).
  5. ACP captures ``main..sandbox-<name>/main`` diff.
  6. ACP runs verification/gates/report (existing pipeline).
  7. ACP stops/removes the sandbox when requested.

Security properties:
  - The agent cannot touch the host filesystem (microVM isolation).
  - The agent cannot reach the network unless the policy allows it.
  - Credentials are injected by a host proxy, never stored inside the
    sandbox.
  - The diff comes from the sandbox remote, not host worktree mutation.

See: https://docs.docker.com/ai/sandboxes/get-started/
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.models import AgentResult


class SbxNotInstalledError(AgentConfigError):
    """Raised when ``sbx`` is not found on PATH."""


@dataclass
class SandboxInfo:
    """Metadata about a started sandbox, recorded into evidence."""

    backend: str = "docker_sbx"
    sbx_version: str = ""
    agent: str = ""
    clone_mode: bool = True
    network_policy: str = "locked_down"
    sandbox_name: str = ""
    host_repo_mode: str = "read_only"
    sandbox_remote: str = ""
    secrets_used_by_name: list[str] = field(default_factory=list)
    secrets_values_recorded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "sbx_version": self.sbx_version,
            "agent": self.agent,
            "clone_mode": self.clone_mode,
            "network_policy": self.network_policy,
            "sandbox_name": self.sandbox_name,
            "host_repo_mode": self.host_repo_mode,
            "sandbox_remote": self.sandbox_remote,
            "secrets_used_by_name": self.secrets_used_by_name,
            "secrets_values_recorded": self.secrets_values_recorded,
        }


class SbxExecutor:
    """Manages a Docker Sandbox for a single ACP task run.

    The executor is constructed with the repo's ``ExecutorSection`` config.
    It does NOT run at construction time — call ``start()`` to launch the
    sandbox and ``stop()`` / ``remove()`` to clean up.
    """

    def __init__(self, config: ExecutorSection) -> None:
        self.config = config
        self._sandbox_name: str = ""
        self._sandbox_remote: str = ""
        self._sbx_version: str = ""

    @property
    def backend_name(self) -> str:
        return "docker_sbx"

    # ------------------------------------------------------------------ #
    # Utility: sbx presence + version
    # ------------------------------------------------------------------ #

    @staticmethod
    def check_installed() -> bool:
        """Return True if ``sbx`` is on PATH."""
        return shutil.which("sbx") is not None

    @staticmethod
    def get_version() -> str:
        """Return the ``sbx`` version string, or empty if not installed."""
        try:
            # sbx uses "sbx version" (not "--version").
            proc = subprocess.run(
                ["sbx", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() or proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    # ------------------------------------------------------------------ #
    # Sandbox name + remote
    # ------------------------------------------------------------------ #

    def sandbox_name(self, task_id: str) -> str:
        """Derive the sandbox name from the prefix + task ID."""
        prefix = self.config.sandbox_name_prefix or "acp"
        # task_id may contain slashes or other chars unsafe for sbx names.
        safe = task_id.replace("/", "-").replace("_", "-")
        return f"{prefix}-{safe}"

    def sandbox_remote(self, task_id: str) -> str:
        """The Git remote name the sandbox exposes on the host."""
        return f"sandbox-{self.sandbox_name(task_id)}"

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def _validate(self) -> None:
        """Fail-closed checks before starting a sandbox."""
        if not self.check_installed():
            raise SbxNotInstalledError(
                "executor.backend='docker_sbx' requires the 'sbx' CLI "
                "(Docker Sandboxes). Install it from "
                "https://docs.docker.com/ai/sandboxes/get-started/"
            )
        if not self.config.clone_mode:
            raise AgentConfigError(
                "executor.clone_mode=False is not allowed. ACP requires "
                "clone mode so the agent works in an isolated private clone, "
                "not the host working tree."
            )
        # v0.5.15: Strict enum validation for network_policy.
        # Only locked_down and balanced are allowed. "open" and any
        # arbitrary string are rejected.
        allowed_policies = ("locked_down", "balanced")
        if self.config.network_policy not in allowed_policies:
            raise AgentConfigError(
                f"executor.network_policy='{self.config.network_policy}' is not valid. "
                f"Allowed values: {', '.join(allowed_policies)}. "
                f"'open' is never allowed — ACP enforces network restrictions."
            )
        if not self.config.agent:
            raise AgentConfigError(
                "executor.agent is required when backend='docker_sbx'. "
                "Specify the agent to run inside the sandbox (e.g. 'claude')."
            )

    # ------------------------------------------------------------------ #
    # Start the sandbox + run the agent
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
        """Start the sandbox, run the agent, and return the result.

        Constructs and runs::

            sbx run --clone --name <name> <agent>

        The prompt content is piped to the agent via stdin. The command
        runs from the repo directory so the sandbox can access the repo.
        """
        self._validate()
        self._sandbox_name = self.sandbox_name(task_id)
        self._sandbox_remote = self.sandbox_remote(task_id)
        self._sbx_version = self.get_version()

        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"

        # Build the sbx command.
        # v0.5.15: Pass the network policy to sbx so it's actually enforced
        # at the runtime layer, not just recorded in evidence.
        cmd = [
            "sbx", "run",
            "--clone",
            "--name", self._sandbox_name,
            "--network", self.config.network_policy,
            self.config.agent,
        ]

        # Read the prompt to pipe via stdin.
        prompt_content = ""
        try:
            prompt_content = prompt_path.read_text()
        except Exception:  # noqa: BLE001
            pass

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                input=prompt_content,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            err = f"acp: sandbox agent timed out after {timeout_seconds}s\n{err}"
            # Best-effort: stop the sandbox on timeout.
            self.stop()
        except FileNotFoundError:
            exit_code = 127
            out, err = "", "acp: 'sbx' not found on PATH"
        except Exception as exc:  # noqa: BLE001
            exit_code = 127
            out, err = "", f"acp: failed to start sandbox: {exc}"

        duration = time.monotonic() - start
        stdout_path.write_text(out)
        stderr_path.write_text(err)

        return AgentResult(
            agent_name=f"sbx:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary=(
                f"sbx agent '{self.config.agent}' ran in sandbox "
                f"'{self._sandbox_name}' (exit {exit_code}, {duration:.2f}s)"
            ),
        )

    # ------------------------------------------------------------------ #
    # Fetch the sandbox remote (for diff capture)
    # ------------------------------------------------------------------ #

    def fetch_remote(self, repo_path: Path) -> str:
        """Fetch the sandbox remote into the host repo.

        Returns the remote name. Raises if the fetch fails.
        """
        if not self._sandbox_remote:
            raise RuntimeError("sandbox has not been started yet")
        try:
            subprocess.run(
                ["git", "fetch", self._sandbox_remote],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"failed to fetch sandbox remote '{self._sandbox_remote}': "
                f"{exc.stderr or exc.stdout or exc}"
            ) from exc
        return self._sandbox_remote

    # ------------------------------------------------------------------ #
    # Sandbox metadata for evidence
    # ------------------------------------------------------------------ #

    def sandbox_info(self, task_id: str) -> SandboxInfo:
        """Build the sandbox metadata record for evidence events."""
        return SandboxInfo(
            sbx_version=self._sbx_version or self.get_version(),
            agent=self.config.agent,
            clone_mode=self.config.clone_mode,
            network_policy=self.config.network_policy,
            sandbox_name=self.sandbox_name(task_id),
            host_repo_mode="read_only",
            sandbox_remote=self.sandbox_remote(task_id),
            secrets_used_by_name=[],
            secrets_values_recorded=False,
        )

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def stop(self) -> bool:
        """Stop the sandbox (preserves state for restart). Returns success."""
        if not self._sandbox_name:
            return False
        try:
            subprocess.run(
                ["sbx", "stop", self._sandbox_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def remove(self) -> bool:
        """Remove the sandbox (deletes everything inside it). Returns success."""
        if not self._sandbox_name:
            return False
        try:
            subprocess.run(
                ["sbx", "rm", self._sandbox_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def cleanup(self) -> None:
        """Stop and optionally remove the sandbox based on config."""
        if self.config.remove_after_run:
            self.remove()
        else:
            self.stop()
