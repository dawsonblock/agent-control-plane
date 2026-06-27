"""macOS seatbelt executor — lightweight OS-level sandbox via ``sandbox-exec``.

v0.9.0 (Step 4): Runs the coding agent inside a macOS seatbelt sandbox
(``sandbox-exec``). This provides kernel-enforced filesystem and network
restrictions without the overhead of a container runtime or microVM.

The seatbelt sandbox is macOS's built-in Mandatory Access Control (MAC)
system, the same mechanism used to sandbox iOS apps and macOS system
services. It operates at the syscall layer — the sandboxed process cannot
escape even if it tries to exec arbitrary binaries.

**When to use this backend:**
  - macOS-only development environments (laptops, CI runners on macOS).
  - When you need stronger isolation than ``worktree`` (which provides none)
    but don't want the overhead of ``docker_sbx`` or ``firecracker``.
  - As a lighter alternative to ``venv`` for non-Python agents (shell,
    custom) that still need OS-level filesystem/network restrictions.

**Sandbox profile:**
  A sandbox profile (Scheme-like DSL) is generated dynamically based on the
  worktree path and ``network_policy``. The default profile:
    - Denies all filesystem writes except the worktree and temp dirs.
    - Allows read access to system paths, the repo, and the worktree.
    - Denies all network access when ``network_policy="locked_down"``.
    - Allows process execution (the agent and its children).

  A custom profile can be provided via ``executor.seatbelt_profile_path``.
  When a custom profile is used, the dynamic generation is skipped — the
  operator is responsible for the profile's security properties.

**Limitations:**
  - macOS-only. Raises ``SeatbeltNotAvailableError`` on non-macOS platforms.
  - ``sandbox-exec`` is deprecated by Apple but still functional and widely
    used (e.g., by Homebrew, Chromium). The newer ``Endpoint Security``
    framework requires a privileged system extension and is not suitable
    for a per-process executor.
  - The sandbox profile is applied to the agent process and its children.
    It does not sandbox the parent ACP process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.models import AgentResult

logger = logging.getLogger(__name__)

_SIGTERM_GRACE_SECONDS = 5


class SeatbeltNotAvailableError(Exception):
    """Raised when sandbox-exec is not available or platform is not macOS."""


class SeatbeltExecutor:
    """Runs the agent inside a macOS seatbelt sandbox (``sandbox-exec``).

    Implements the :class:`Executor` protocol. The agent command is wrapped
    in ``sandbox-exec -f <profile> -- <command>``, where the profile is
    generated dynamically (or loaded from a custom path) to restrict
    filesystem and network access.
    """

    def __init__(self, config: ExecutorSection) -> None:
        self.config = config
        self._proc: subprocess.Popen[str] | None = None
        self._profile_path: Path | None = None

    @property
    def backend_name(self) -> str:
        return "seatbelt"

    # ------------------------------------------------------------------ #
    # Pre-flight validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def check_installed() -> bool:
        """Return True if ``sandbox-exec`` is available and platform is macOS."""
        if sys.platform != "darwin":
            return False
        from shutil import which

        return which("sandbox-exec") is not None

    def _validate(self) -> None:
        """Fail-closed checks before starting a seatbelt-sandboxed run."""
        if sys.platform != "darwin":
            raise SeatbeltNotAvailableError(
                f"executor.backend='seatbelt' requires macOS, got platform={sys.platform}"
            )
        from shutil import which

        if which("sandbox-exec") is None:
            raise SeatbeltNotAvailableError(
                "executor.backend='seatbelt' requires sandbox-exec on PATH. "
                "This tool is part of macOS and should be at /usr/bin/sandbox-exec."
            )
        if not self.config.agent:
            raise AgentConfigError(
                "executor.agent is required when backend='seatbelt'. "
                "Specify the command to run inside the sandbox."
            )

    def get_environment_info(self) -> dict[str, str]:
        """Return environment metadata for the agent.started event payload."""
        return {
            "backend": "seatbelt",
            "platform": platform.platform(),
            "network_policy": self.config.network_policy,
            "profile": "custom" if self.config.seatbelt_profile_path else "auto-generated",
        }

    # ------------------------------------------------------------------ #
    # Sandbox profile generation
    # ------------------------------------------------------------------ #

    def _generate_profile(
        self,
        worktree_path: Path,
        repo_path: Path,
        artifact_dir: Path,
    ) -> str:
        """Generate a seatbelt sandbox profile (Scheme DSL) for the run.

        The profile denies everything by default, then allows:
          - Process execution (the agent and its children).
          - File reads from system paths, the repo, and the worktree.
          - File writes only to the worktree, artifact dir, and temp dirs.
          - Network access based on ``network_policy``.
        """
        wt = str(worktree_path.resolve())
        repo = str(repo_path.resolve())
        artifacts = str(artifact_dir.resolve())

        # System paths that the agent needs to read (binaries, libs, etc.).
        read_paths = [
            "/usr",
            "/System",
            "/Library",
            "/bin",
            "/sbin",
            "/opt",
            "/dev",
            "/etc",
            "/var/db",
            "/private/etc",
            "/private/var/db",
            repo,
            wt,
            artifacts,
        ]

        # Paths the agent can write to (worktree + temp + artifacts).
        write_paths = [wt, artifacts, "/tmp", "/var/tmp", "/private/tmp"]

        # Network rules based on network_policy.
        if self.config.network_policy == "open":
            network_rules = "(allow network*)"
        elif self.config.network_policy == "balanced":
            # Balanced: allow DNS resolution + outbound TCP/UDP only.
            # Inbound connections are denied. This is coarser than a custom
            # profile but more restrictive than "open".
            network_rules = """
            (allow network-outbound*)
            (allow network-DNS)
            (deny network-inbound*)
            """
        else:
            # locked_down: deny all network.
            network_rules = "(deny network*)"

        def _escape_path(p: str) -> str:
            """Escape backslashes and double quotes for the seatbelt DSL."""
            return p.replace("\\", "\\\\").replace('"', '\\"')

        # Build the profile.
        lines = ["(version 1)", "(deny default)"]

        # Allow process execution.
        lines.append("(allow process*)")
        lines.append("(allow process-info*)")

        # Allow file reads from system + repo + worktree.
        for p in read_paths:
            ep = _escape_path(p)
            lines.append(f'(allow file-read* (subpath "{ep}"))')

        # Allow file writes to worktree + temp + artifacts.
        for p in write_paths:
            ep = _escape_path(p)
            lines.append(f'(allow file-write* (subpath "{ep}"))')

        # Allow signals and IPC.
        lines.append("(allow signal)")
        lines.append("(allow mach*)")
        lines.append("(allow sysctl-read)")

        # Network rules.
        lines.append(network_rules)

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ #
    # Run the agent
    # ------------------------------------------------------------------ #

    async def start(
        self,
        *,
        task_id: str,
        prompt_path: Path,
        repo_path: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> AgentResult:
        """Run the agent inside a seatbelt sandbox and return the result."""
        self._validate()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"

        # Determine the worktree path (the agent's cwd).
        # For seatbelt, we use the repo_path as the working directory since
        # the worktree is already created by the workflow graph. The sandbox
        # profile restricts writes to this path.
        worktree_path = repo_path

        # Generate or load the sandbox profile.
        if self.config.seatbelt_profile_path:
            profile_path = Path(self.config.seatbelt_profile_path)
            if not profile_path.is_file():
                return self._error_result(
                    stdout_path,
                    stderr_path,
                    127,
                    "",
                    f"seatbelt: custom profile not found: {profile_path}",
                )
            self._profile_path = profile_path
        else:
            profile_text = self._generate_profile(worktree_path, repo_path, artifact_dir)
            # Write the profile to a temp file.
            fd, profile_tmp = tempfile.mkstemp(suffix=".sb", prefix="acp_seatbelt_")
            os.write(fd, profile_text.encode())
            os.close(fd)
            self._profile_path = Path(profile_tmp)

        # Build the sandbox-exec command.
        agent_command = self.config.agent
        cmd = [
            "sandbox-exec",
            "-f",
            str(self._profile_path),
            "--",
            *shlex.split(agent_command),
        ]

        logger.info(
            "seatbelt: starting sandboxed agent for task %s (profile=%s, network=%s)",
            task_id,
            self._profile_path,
            self.config.network_policy,
        )

        try:
            prompt_content = prompt_path.read_text()
        except OSError as exc:
            return self._error_result(
                stdout_path,
                stderr_path,
                127,
                "",
                f"seatbelt: cannot read prompt: {exc}",
            )

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(worktree_path),
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return self._error_result(
                stdout_path,
                stderr_path,
                127,
                "",
                f"seatbelt: sandbox-exec not found: {exc}",
            )

        try:
            out, err = await asyncio.to_thread(
                self._proc.communicate, input=prompt_content, timeout=timeout_seconds
            )
            exit_code = self._proc.returncode
        except subprocess.TimeoutExpired:
            self.stop()
            try:
                out, err = await asyncio.to_thread(self._proc.communicate, timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", f"seatbelt: agent timed out after {timeout_seconds}s"
            else:
                err = (err or "") + f"\nseatbelt: agent timed out after {timeout_seconds}s"
            exit_code = 124
        finally:
            self._proc = None
            # Clean up the temp profile if we generated it.
            if self._profile_path and not self.config.seatbelt_profile_path:
                try:
                    self._profile_path.unlink(missing_ok=True)
                except OSError:
                    pass

        stdout_path.write_text(out or "")
        stderr_path.write_text(err or "")

        return AgentResult(
            agent_name=f"seatbelt:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def _error_result(
        self,
        stdout_path: Path,
        stderr_path: Path,
        exit_code: int,
        out: str,
        err: str,
    ) -> AgentResult:
        """Write captured output and return an AgentResult for error cases."""
        stdout_path.write_text(out)
        stderr_path.write_text(err)
        return AgentResult(
            agent_name=f"seatbelt:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def stop(self) -> bool:
        """Stop the running agent process (SIGTERM → SIGKILL escalation)."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return True

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return True

        try:
            proc.wait(timeout=_SIGTERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning("seatbelt: process %d did not exit after SIGKILL", proc.pid)
                return False
        return True

    def cleanup(self) -> None:
        """Clean up resources. Removes the temp profile if generated."""
        if self._profile_path and not self.config.seatbelt_profile_path:
            try:
                self._profile_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._profile_path = None

    def fetch_remote(self) -> str:
        """Fetch the agent's git remote. Not applicable for seatbelt."""
        return ""
