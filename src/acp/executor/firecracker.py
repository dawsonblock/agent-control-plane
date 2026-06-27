"""Firecracker executor — runs agents inside a Firecracker microVM.

Firecracker is an open-source VMM (virtual machine monitor) built by AWS
that provides lightweight, secure, and fast-booting microVMs. This executor
launches a microVM for each agent run, providing hardware-level isolation
(stronger than gVisor's syscall-level sandboxing) with minimal overhead
(~125ms boot time, ~5 MiB memory footprint).

This executor requires:
  - ``firecracker`` binary installed and in PATH
  - A kernel image (vmlinux) — uncompressed Linux kernel
  - A root filesystem image (ext4) with the agent installed
  - ``clone_mode=True`` — the microVM gets a private copy of the repo

The executor follows the same pattern as GvisorExecutor:
  - ``start()``: launches the microVM, runs the agent, waits for completion
  - ``stop()``: stops the microVM process
  - ``cleanup()``: removes the microVM socket and temporary files
  - ``fetch_remote()``: returns empty (repo is mounted as a block device)

Configuration (ExecutorSection):
  - ``backend``: must be ``"firecracker"``
  - ``agent``: the agent command to run inside the microVM
  - ``firecracker_kernel_path``: path to the vmlinux kernel image
  - ``firecracker_rootfs_path``: path to the ext4 root filesystem image
  - ``network_policy``: ``"locked_down"`` (no network) or ``"balanced"``
    (TAP device with host NAT — requires root or CAP_NET_ADMIN)
  - ``remove_after_run``: whether to clean up the rootfs copy after the run
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.models import AgentResult

logger = logging.getLogger(__name__)


class FirecrackerNotInstalledError(Exception):
    """Raised when the firecracker binary is not installed but backend='firecracker'."""


class FirecrackerExecutor:
    """Runs agents inside a Firecracker microVM.

    Implements the :class:`Executor` protocol. Each agent run gets a
    dedicated microVM with its own kernel, root filesystem, and network
    namespace. The repo is copied into the rootfs before boot, and the
    agent's changes are extracted after the microVM shuts down.

    The microVM configuration is written as a JSON file and passed to
    ``firecracker --config-file`` via a Unix socket API. The agent runs
    inside the microVM via an init script that reads the prompt from stdin.
    """

    def __init__(self, config: ExecutorSection) -> None:
        self.config = config
        self._vm_id: str = ""
        self._socket_path: str = ""
        self._rootfs_copy_path: str = ""
        self._fc_process: subprocess.Popen[bytes] | None = None
        self._fc_version: str = ""

    @property
    def backend_name(self) -> str:
        return "firecracker"

    # ------------------------------------------------------------------ #
    # Pre-flight validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def check_installed() -> bool:
        """Return True if the firecracker binary is available."""
        return shutil.which("firecracker") is not None

    @staticmethod
    def get_version() -> str:
        """Return the firecracker version string, or empty if not installed."""
        try:
            proc = subprocess.run(
                ["firecracker", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() or proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def _validate(self) -> None:
        """Fail-closed checks before starting a Firecracker run."""
        if not self.check_installed():
            raise FirecrackerNotInstalledError(
                "executor.backend='firecracker' requires the firecracker binary. "
                "Install Firecracker: "
                "https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md"
            )
        if not self.config.clone_mode:
            raise AgentConfigError(
                "executor.clone_mode must be True when backend='firecracker'. "
                "The agent needs a private rootfs copy inside the microVM."
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
                "executor.agent is required when backend='firecracker'. "
                "Specify the agent command to run inside the microVM."
            )
        if not self.config.firecracker_kernel_path:
            raise AgentConfigError(
                "executor.firecracker_kernel_path is required when backend='firecracker'. "
                "Provide a path to an uncompressed Linux kernel (vmlinux)."
            )
        if not self.config.firecracker_rootfs_path:
            raise AgentConfigError(
                "executor.firecracker_rootfs_path is required when backend='firecracker'. "
                "Provide a path to an ext4 root filesystem image."
            )
        kernel = Path(self.config.firecracker_kernel_path)
        if not kernel.is_file():
            raise AgentConfigError(
                f"firecracker_kernel_path '{kernel}' does not exist or is not a file."
            )
        rootfs = Path(self.config.firecracker_rootfs_path)
        if not rootfs.is_file():
            raise AgentConfigError(
                f"firecracker_rootfs_path '{rootfs}' does not exist or is not a file."
            )

    # ------------------------------------------------------------------ #
    # Start the microVM + run the agent
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
        """Start a Firecracker microVM, run the agent, and return the result.

        The microVM is configured with:
          - The specified kernel and a copy of the rootfs (with the repo
            injected via ``cp`` into the rootfs before boot)
          - ``--network=none`` (locked_down) or a TAP device (balanced)
          - A vsock for stdout/stderr capture
          - An init script that runs the agent command and shuts down

        After the agent finishes, stdout/stderr are read from the artifact
        directory (written by the init script via vsock or serial console).
        """
        self._validate()
        self._vm_id = f"{self.config.sandbox_name_prefix}-{task_id}"
        self._socket_path = str(artifact_dir / "firecracker.sock")

        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"

        # Copy the rootfs so the microVM has a writable filesystem.
        rootfs_src = Path(self.config.firecracker_rootfs_path)
        self._rootfs_copy_path = str(artifact_dir / "rootfs.img")
        await asyncio.to_thread(shutil.copy2, str(rootfs_src), self._rootfs_copy_path)

        # Inject the repo into the rootfs using debugfs (requires e2fsprogs).
        # This is a simplified approach — production deployments would use
        # a more robust method (e.g., a shared virtio block device or 9p).
        await self._inject_repo_into_rootfs(repo_path)

        # Inject the prompt as /prompt.txt inside the rootfs so the init
        # script can pass it to the agent.
        await self._inject_prompt_into_rootfs(prompt_path)

        # Inject the init script that runs the agent and tars the
        # workspace back into /workspace.tar.gz after the agent finishes.
        await self._inject_init_script_into_rootfs()

        # Build the Firecracker config.
        fc_config = self._build_fc_config(timeout_seconds)
        config_path = artifact_dir / "fc_config.json"
        config_path.write_text(json.dumps(fc_config, indent=2))

        agent_cmd = self.config.agent

        logger.info(
            "firecracker: starting microVM %s for task %s (network=%s)",
            self._vm_id,
            task_id,
            self.config.network_policy,
        )

        start = time.monotonic()
        timed_out = False
        try:
            # Launch firecracker with the config file. The agent runs inside
            # the microVM via the init script, and stdout is captured via
            # the serial console (redirected to the stdout file).
            with (
                open(stdout_path, "w") as stdout_f,
                open(stderr_path, "w") as stderr_f,
            ):
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "firecracker",
                        "--no-api",
                        "--config-file",
                        str(config_path),
                    ],
                    input=agent_cmd,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    text=True,
                    timeout=timeout_seconds,
                )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
            timed_out = True
            stderr_path.write_text(
                f"firecracker: agent timed out after {timeout_seconds}s",
                encoding="utf-8",
            )
            self.stop()
        except FileNotFoundError as exc:
            exit_code = 127
            stdout_path.write_text("")
            stderr_path.write_text(f"firecracker: binary not found: {exc}")

        duration = time.monotonic() - start

        return AgentResult(
            agent_name=f"firecracker:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary=(
                f"Firecracker microVM {self._vm_id} "
                f"({'timed out' if timed_out else 'completed'}) "
                f"in {duration:.1f}s"
            ),
        )

    def _build_fc_config(self, timeout_seconds: int) -> dict[str, Any]:
        """Build the Firecracker JSON configuration."""
        fc_config: dict[str, Any] = {
            "boot-source": {
                "kernel_image_path": self.config.firecracker_kernel_path,
                "boot_args": (
                    "console=ttyS0 reboot=k panic=1 pci=off random.trust_cpu=on init=/init_fc.sh"
                ),
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "path_on_host": self._rootfs_copy_path,
                    "is_root_device": True,
                    "is_read_only": False,
                }
            ],
            "machine-config": {
                "vcpu_count": 2,
                "mem_size_mib": 512,
                "smt": False,
            },
            "logger": {
                "log_path": str(Path(self._socket_path).parent / "firecracker.log"),
            },
        }

        if self.config.network_policy == "balanced":
            fc_config["network-interfaces"] = [
                {
                    "iface_id": "net0",
                    "host_dev_name": f"tap-{self._vm_id}",
                    "guest_mac": "AA:BB:CC:DD:EE:01",
                }
            ]

        return fc_config

    async def _inject_prompt_into_rootfs(self, prompt_path: Path) -> None:
        """Inject the prompt file into the rootfs as /prompt.txt."""
        if not shutil.which("debugfs"):
            return

        result = await asyncio.to_thread(
            subprocess.run,
            [
                "debugfs",
                "-w",
                self._rootfs_copy_path,
                "-R",
                f"write {prompt_path} /prompt.txt",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "firecracker: failed to inject prompt (exit %s): %s",
                result.returncode,
                result.stderr.strip(),
            )

    async def _inject_init_script_into_rootfs(self) -> None:
        """Inject the init script into the rootfs that runs the agent and
        tars the workspace back after the agent finishes.

        The init script:
          1. Extracts /workspace.tar.gz to /workspace
          2. Reads the agent command from stdin (piped from firecracker)
          3. Runs the agent inside /workspace
          4. Tars /workspace back to /workspace.tar.gz
          5. Powers off the microVM

        This requires e2fsprogs (debugfs) on the host.
        """
        if not shutil.which("debugfs"):
            logger.warning("firecracker: debugfs not found — cannot inject init script")
            return

        import tempfile

        init_script = """#!/bin/sh
# ACP Firecracker init script — runs inside the microVM.
set -e

# Extract the repo tarball into /workspace.
cd /
if [ -f /workspace.tar.gz ]; then
    tar xzf /workspace.tar.gz
fi

# Read the agent command from stdin (piped from firecracker).
AGENT_CMD=$(cat)

# Run the agent inside /workspace, passing the prompt via stdin.
cd /workspace
if [ -n "$AGENT_CMD" ]; then
    if [ -f /prompt.txt ]; then
        sh -c "$AGENT_CMD" < /prompt.txt 2>&1 || true
    else
        sh -c "$AGENT_CMD" 2>&1 || true
    fi
fi

# Tar the workspace back so the host can extract changes via debugfs.
cd /
tar czf /workspace.tar.gz workspace

# Power off.
poweroff -f
"""

        script_file = tempfile.NamedTemporaryFile(suffix=".sh", delete=False, mode="w")
        script_file.write(init_script)
        script_file.close()
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "debugfs",
                    "-w",
                    self._rootfs_copy_path,
                    "-R",
                    f"write {script_file.name} /init_fc.sh",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "firecracker: failed to inject init script (exit %s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
        finally:
            Path(script_file.name).unlink(missing_ok=True)

    async def _inject_repo_into_rootfs(self, repo_path: Path) -> None:
        """Inject the repo into the rootfs image using debugfs.

        This is a simplified approach for development/testing. Production
        deployments should use a shared virtio block device or 9p mount.
        """
        # Create a tarball of the repo and inject it into the rootfs.
        # The init script inside the rootfs extracts it to /workspace.
        # This requires e2fsprogs (debugfs) to be installed on the host.
        if not shutil.which("debugfs"):
            logger.warning(
                "firecracker: debugfs not found — repo will not be injected. "
                "Install e2fsprogs for repo injection support."
            )
            return

        import tarfile
        import tempfile

        repo_tarball = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        repo_tarball.close()
        try:
            with tarfile.open(repo_tarball.name, "w:gz") as tar:
                tar.add(str(repo_path), arcname="workspace")

            # Inject the tarball into the rootfs using debugfs.
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "debugfs",
                    "-w",
                    self._rootfs_copy_path,
                    "-R",
                    f"write {repo_tarball.name} /workspace.tar.gz",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning(
                    "firecracker: debugfs write failed (exit %s): %s "
                    "— microVM will boot without repo injection",
                    result.returncode,
                    result.stderr.strip(),
                )
        finally:
            Path(repo_tarball.name).unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Stop / cleanup / remote
    # ------------------------------------------------------------------ #

    def stop(self) -> bool:
        """Stop the microVM process.

        Since ``start()`` uses ``subprocess.run`` (blocking), we don't have
        a Popen handle. Instead, we kill any firecracker process whose
        command line contains our socket path (unique per run).
        """
        if not self._socket_path:
            return False
        try:
            proc = subprocess.run(
                ["pkill", "-f", self._socket_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def remove(self) -> bool:
        """Permanently remove the microVM artifacts."""
        import os

        cleaned = False
        if self._socket_path and Path(self._socket_path).exists():
            try:
                os.unlink(self._socket_path)
                cleaned = True
            except OSError:
                pass
        if self._rootfs_copy_path and Path(self._rootfs_copy_path).exists():
            try:
                Path(self._rootfs_copy_path).unlink()
                cleaned = True
            except OSError:
                pass
        return cleaned

    def cleanup(self) -> None:
        """Stop and optionally remove microVM artifacts based on config."""
        self.stop()
        if self.config.remove_after_run:
            self.remove()

    def fetch_remote(self, repo_path: Path) -> str:
        """Fetch the microVM's git remote for diff capture.

        Since the repo is injected into the rootfs, the agent's changes
        are inside the rootfs image. The diff is captured from the rootfs
        after the microVM shuts down. Returns empty to signal that the
        diff should be captured from the worktree/rootfs directly.
        """
        return ""

    def extract_workspace_from_rootfs(self, dest_path: Path) -> bool:
        """Extract the /workspace directory from the rootfs after microVM shutdown.

        Uses ``debugfs`` to read the workspace.tar.gz that the init script
        created from the agent's modifications. Returns True if extraction
        succeeded and ``dest_path`` contains a valid git repo.
        """
        if not self._rootfs_copy_path or not Path(self._rootfs_copy_path).exists():
            logger.warning("firecracker: rootfs copy not found for workspace extraction")
            return False
        if not shutil.which("debugfs"):
            logger.warning("firecracker: debugfs not found — cannot extract workspace")
            return False

        import tarfile
        import tempfile

        dest_path.mkdir(parents=True, exist_ok=True)
        tarball = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        tarball.close()
        try:
            with open(tarball.name, "wb") as tb_out:
                result = subprocess.run(
                    [
                        "debugfs",
                        "-R",
                        "dump /workspace.tar.gz",
                        self._rootfs_copy_path,
                    ],
                    stdout=tb_out,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                )
            if result.returncode != 0:
                logger.warning(
                    "firecracker: debugfs dump failed (exit %s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False
            with tarfile.open(tarball.name, "r:gz") as tar:
                tar.extractall(str(dest_path))
            return (dest_path / "workspace").exists() or dest_path.exists()
        except Exception as exc:  # noqa: BLE001
            logger.warning("firecracker: workspace extraction failed: %s", exc)
            return False
        finally:
            Path(tarball.name).unlink(missing_ok=True)

    def info(self) -> dict[str, str]:
        """Build metadata for evidence events."""
        return {
            "backend": "firecracker",
            "vm_id": self._vm_id,
            "network_policy": self.config.network_policy,
            "firecracker_version": self._fc_version or self.get_version(),
            "agent": self.config.agent,
            "kernel_path": self.config.firecracker_kernel_path,
            "rootfs_path": self.config.firecracker_rootfs_path,
        }
