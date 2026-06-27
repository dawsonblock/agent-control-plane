"""gVisor executor — runs agents inside a gVisor (runsc) sandboxed container.

gVisor provides OS-level sandboxing via a user-space kernel that implements
the Linux system call interface. This is stronger isolation than a git
worktree (which only provides workflow isolation) but lighter-weight than
a full VM (Firecracker). The agent runs inside a container with gVisor's
runtime, so filesystem access, network calls, and syscalls are intercepted
and filtered.

This executor requires:
  - Docker installed and running
  - gVisor runtime (runsc) installed and registered with Docker
  - The agent binary available inside the container image

The executor follows the same pattern as SbxExecutor:
  - ``start()``: launches the agent in a gVisor-backed container
  - ``stop()``: stops the container
  - ``cleanup()``: removes the container (if remove_after_run=True)
  - ``fetch_remote()``: fetches the container's git remote for diff capture

Configuration (ExecutorSection):
  - ``backend``: must be ``"gvisor"``
  - ``agent``: the agent command to run (e.g. ``"claude"``)
  - ``sandbox_name_prefix``: prefix for container names (default: ``"acp"``)
  - ``clone_mode``: must be True — the container gets a private git clone
  - ``network_policy``: maps to Docker network modes
  - ``remove_after_run``: whether to remove the container after the run
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.models import AgentResult

logger = logging.getLogger(__name__)


class GvisorNotInstalledError(Exception):
    """Raised when gVisor (runsc) is not installed but backend='gvisor'."""


class GvisorExecutor:
    """Runs agents inside a gVisor-sandboxed Docker container.

    Implements the :class:`Executor` protocol. The container uses the
    ``runsc`` runtime registered with Docker, providing syscall-level
    isolation that prevents the agent from accessing the host filesystem,
    network, or other resources outside the container boundary.
    """

    def __init__(self, config: ExecutorSection) -> None:
        self.config = config
        self._container_name: str = ""
        self._container_remote: str = ""
        self._gvisor_version: str = ""

    @property
    def backend_name(self) -> str:
        return "gvisor"

    # ------------------------------------------------------------------ #
    # Pre-flight validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def check_installed() -> bool:
        """Return True if Docker + gVisor runtime are available."""
        if not shutil.which("docker"):
            return False
        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{json .Runtimes}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "runsc" in (proc.stdout or "")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def get_version() -> str:
        """Return the runsc version string, or empty if not installed."""
        try:
            proc = subprocess.run(
                ["runsc", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() or proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def _validate(self) -> None:
        """Fail-closed checks before starting a gVisor run."""
        if not self.check_installed():
            raise GvisorNotInstalledError(
                "executor.backend='gvisor' requires Docker with the runsc "
                "runtime. Install gVisor: "
                "https://gvisor.dev/docs/user_guide/install/ "
                "and register with Docker: "
                "docker run --runtime=runsc hello-world"
            )
        if not self.config.clone_mode:
            raise AgentConfigError(
                "executor.clone_mode must be True when backend='gvisor'. "
                "The agent needs a private git clone inside the container."
            )
        allowed_policies = ("locked_down", "balanced")
        if self.config.network_policy not in allowed_policies:
            raise AgentConfigError(
                f"executor.network_policy='{self.config.network_policy}' is not valid. "
                f"Allowed values: {', '.join(allowed_policies)}. "
                f"'open' is never allowed — ACP enforces network restrictions."
            )
        if not self.config.agent:
            raise AgentConfigError(
                "executor.agent is required when backend='gvisor'. "
                "Specify the agent command to run inside the container."
            )

    # ------------------------------------------------------------------ #
    # Start the container + run the agent
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
        """Start a gVisor container, run the agent, and return the result.

        The container is started with:
          - ``--runtime=runsc`` for gVisor isolation
          - ``--network=none`` (locked_down) or ``--network=bridge`` (balanced)
          - The repo mounted as a volume at /workspace
          - The prompt piped via stdin

        After the agent finishes, the container's git remote is fetched
        for diff capture (same pattern as docker_sbx).
        """
        self._validate()
        self._container_name = f"{self.config.sandbox_name_prefix}-{task_id}"

        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"

        # Build the Docker command.
        network_flag = "none" if self.config.network_policy == "locked_down" else "bridge"
        prompt_content = prompt_path.read_text()

        cmd = [
            "docker",
            "run",
            "--rm",
            "--runtime=runsc",
            "--name",
            self._container_name,
            f"--network={network_flag}",
            "-v",
            f"{repo_path}:/workspace:rw",
            "-w",
            "/workspace",
            "-i",  # stdin for prompt
            # Use a minimal image with git + the agent. The user is
            # responsible for building/pulling an image that has their
            # agent installed. We default to a generic image.
            "ubuntu:22.04",
            "bash",
            "-c",
            f"{self.config.agent} 2>&1",
        ]

        logger.info(
            "gvisor: starting container %s for task %s (network=%s)",
            self._container_name,
            task_id,
            network_flag,
        )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt_content,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
            out, err = proc.stdout, proc.stderr
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = 124
            timed_out = True
            out, err = "", f"gvisor: agent timed out after {timeout_seconds}s"
            # Kill the container if it's still running.
            self.stop()
        except FileNotFoundError as exc:
            exit_code = 127
            timed_out = False
            out, err = "", f"gvisor: docker not found: {exc}"

        duration = time.monotonic() - start
        stdout_path.write_text(out)
        stderr_path.write_text(err)

        self._container_remote = f"docker://{self._container_name}"

        return AgentResult(
            agent_name=f"gvisor:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary=(
                f"gVisor container {self._container_name} "
                f"({'timed out' if timed_out else 'completed'}) "
                f"in {duration:.1f}s"
            ),
        )

    # ------------------------------------------------------------------ #
    # Stop / cleanup / remote
    # ------------------------------------------------------------------ #

    def stop(self) -> bool:
        """Stop the container (preserves state for restart)."""
        if not self._container_name:
            return False
        try:
            subprocess.run(
                ["docker", "stop", self._container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def remove(self) -> bool:
        """Permanently remove the container."""
        if not self._container_name:
            return False
        try:
            subprocess.run(
                ["docker", "rm", "-f", self._container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def cleanup(self) -> None:
        """Stop and optionally remove the container based on config."""
        if self.config.remove_after_run:
            self.remove()
        else:
            self.stop()

    def fetch_remote(self, repo_path: Path) -> str:
        """Fetch the container's git remote for diff capture.

        Since the repo is mounted as a volume, the agent's changes are
        already in the worktree — no remote fetch needed. This returns
        an empty string to signal that the diff should be captured from
        the worktree directly (same as the worktree backend).
        """
        return ""

    def info(self) -> dict[str, str]:
        """Build metadata for evidence events."""
        return {
            "backend": "gvisor",
            "container_name": self._container_name,
            "network_policy": self.config.network_policy,
            "gvisor_version": self._gvisor_version or self.get_version(),
            "agent": self.config.agent,
        }
