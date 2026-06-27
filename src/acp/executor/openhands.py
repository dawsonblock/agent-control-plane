"""OpenHands executor backend (v0.7.0 Phase 2.1).

Runs the OpenHands AI agent in headless mode inside its own Docker-based
runtime. OpenHands is a model-agnostic coding agent that can use any
LiteLLM-supported LLM provider (OpenAI, Anthropic, OpenRouter, DeepSeek,
Ollama, vLLM, etc.).

OpenHands headless mode:

    openhands --headless --json -f <task_file>

The ``--json`` flag enables structured JSONL output, streaming events
as they occur. The ``-f`` flag loads the task from a file. Headless
mode always runs in ``always-approve`` mode — the agent executes all
actions without confirmation.

Flow:
  1. ACP creates the task/run/evidence directory.
  2. OpenHandsExecutor starts ``openhands --headless --json -f <prompt>``.
  3. The agent works inside its Docker runtime sandbox.
  4. OpenHands writes changes to the working directory (the worktree).
  5. ACP captures the diff from the worktree (existing pipeline).
  6. ACP runs verification/gates/report (existing pipeline).
  7. OpenHandsExecutor.cleanup() stops the runtime.

Security properties:
  - The agent runs inside OpenHands' Docker sandbox (not the host).
  - Network access is controlled by OpenHands' runtime config.
  - The agent's stdout (JSONL event stream) is captured as evidence.
  - The executor emits executor.started and executor.finished events.

Requirements:
  - ``openhands`` CLI installed (``pip install openhands`` or
    ``uv pip install openhands``).
  - Docker installed (OpenHands uses Docker for its runtime).
  - LLM API key configured (LLM_API_KEY, LLM_MODEL env vars or
    OpenHands config file).

See: https://docs.openhands.dev/openhands/usage/cli/headless
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.models import AgentResult


class OpenHandsNotInstalledError(AgentConfigError):
    """Raised when ``openhands`` is not found on PATH."""


@dataclass
class OpenHandsInfo:
    """Metadata about an OpenHands run, recorded into evidence."""

    backend: str = "openhands"
    openhands_version: str = ""
    model: str = ""
    runtime: str = "docker"
    headless: bool = True
    json_output: bool = True
    working_dir: str = ""
    events_captured: int = 0
    secrets_values_recorded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "openhands_version": self.openhands_version,
            "model": self.model,
            "runtime": self.runtime,
            "headless": self.headless,
            "json_output": self.json_output,
            "working_dir": self.working_dir,
            "events_captured": self.events_captured,
            "secrets_values_recorded": self.secrets_values_recorded,
        }


class OpenHandsExecutor:
    """Runs the OpenHands agent in headless mode.

    The executor is constructed with the repo's ``ExecutorSection`` config.
    It does NOT run at construction time — call ``start()`` to launch
    the agent and ``cleanup()`` to clean up.

    The OpenHands agent works directly in the ACP worktree (unlike
    docker_sbx which uses a clone inside the microVM). OpenHands' own
    Docker runtime provides the isolation boundary.
    """

    def __init__(self, config: ExecutorSection) -> None:
        self.config = config
        self._version: str = ""
        self._events_captured: int = 0
        self._working_dir: str = ""

    @property
    def backend_name(self) -> str:
        return "openhands"

    # ------------------------------------------------------------------ #
    # Utility: openhands presence + version
    # ------------------------------------------------------------------ #

    @staticmethod
    def check_installed() -> bool:
        """Return True if ``openhands`` is on PATH."""
        return shutil.which("openhands") is not None

    @staticmethod
    def get_version() -> str:
        """Return the ``openhands`` version string, or empty if not installed."""
        try:
            proc = subprocess.run(
                ["openhands", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.stdout.strip() or proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def _validate(self) -> None:
        """Fail-closed checks before starting OpenHands."""
        if not self.check_installed():
            raise OpenHandsNotInstalledError(
                "executor.backend='openhands' requires the 'openhands' CLI. "
                "Install it with: pip install openhands "
                "(see https://docs.openhands.dev)"
            )
        if not self.config.agent:
            raise AgentConfigError(
                "executor.agent is required when backend='openhands'. "
                "Specify the LLM model to use (e.g. 'claude-sonnet-4-20250514')."
            )

    # ------------------------------------------------------------------ #
    # Start the agent
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
        """Start OpenHands in headless mode and return the result.

        Constructs and runs::

            openhands --headless --json -f <prompt_file>

        The agent works in the ``repo_path`` directory (the ACP worktree).
        stdout (JSONL event stream) and stderr are captured to the
        artifact directory.
        """
        self._validate()
        self._version = self.get_version()
        self._working_dir = str(repo_path)

        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"
        events_path = artifact_dir / "openhands_events.jsonl"

        # Build the openhands command.
        # OpenHands CLI does NOT have a --model flag. Models are configured
        # via the settings UI, config file, or environment variables with
        # --override-with-envs. We use the env var approach so the ACP
        # config's agent field (used as the LLM model name) is respected.
        cmd = [
            "openhands",
            "--headless",
            "--json",
            "-f",
            str(prompt_path),
        ]

        # Pass the model via LLM_MODEL env var with --override-with-envs.
        # The agent field is used as the LLM model name for OpenHands.
        env = os.environ.copy()
        if self.config.agent:
            env["LLM_MODEL"] = self.config.agent
            cmd.append("--override-with-envs")

        # Set the working directory to the repo path (the worktree).
        # OpenHands works in the current directory by default.

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            err = f"acp: openhands agent timed out after {timeout_seconds}s\n{err}"
        except FileNotFoundError:
            exit_code = 127
            out, err = "", "acp: 'openhands' not found on PATH"
        except Exception as exc:  # noqa: BLE001
            exit_code = 127
            out, err = "", f"acp: failed to start openhands: {exc}"

        duration = time.monotonic() - start
        stdout_path.write_text(out)
        stderr_path.write_text(err)

        # Parse the JSONL event stream to count events.
        self._events_captured = self._count_jsonl_events(out)
        # Also write a clean events file (just the JSONL lines).
        self._extract_jsonl_events(out, events_path)

        return AgentResult(
            agent_name=f"openhands:{self.config.agent}",
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary=(
                f"openhands agent '{self.config.agent}' ran in "
                f"'{repo_path}' (exit {exit_code}, {duration:.2f}s, "
                f"{self._events_captured} events)"
            ),
        )

    # ------------------------------------------------------------------ #
    # JSONL event parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _count_jsonl_events(stdout: str) -> int:
        """Count valid JSONL lines in the stdout."""
        count = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
                count += 1
            except json.JSONDecodeError:
                continue
        return count

    @staticmethod
    def _extract_jsonl_events(stdout: str, output_path: Path) -> None:
        """Extract valid JSONL lines from stdout and write to a file."""
        lines = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
                lines.append(line)
            except json.JSONDecodeError:
                continue
        if lines:
            output_path.write_text("\n".join(lines) + "\n")

    # ------------------------------------------------------------------ #
    # Metadata for evidence
    # ------------------------------------------------------------------ #

    def info(self) -> OpenHandsInfo:
        """Build the executor metadata record for evidence events."""
        return OpenHandsInfo(
            openhands_version=self._version or self.get_version(),
            model=self.config.agent,
            runtime="docker",
            headless=True,
            json_output=True,
            working_dir=self._working_dir,
            events_captured=self._events_captured,
            secrets_values_recorded=False,
        )

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def stop(self) -> bool:
        """Stop the OpenHands runtime (if running).

        OpenHands headless mode runs as a subprocess — when the subprocess
        finishes, the runtime is automatically cleaned up. There's no
        persistent sandbox to stop (unlike docker_sbx).
        """
        return True

    def cleanup(self) -> None:
        """Clean up resources after the run.

        OpenHands headless mode doesn't leave persistent resources —
        the Docker runtime is cleaned up when the subprocess exits.
        This is a no-op for the openhands backend.
        """
        pass

    # ------------------------------------------------------------------ #
    # Fetch remote (compatibility with SbxExecutor interface)
    # ------------------------------------------------------------------ #

    def fetch_remote(self, repo_path: Path) -> str:
        """No-op for OpenHands — the agent works directly in the worktree.

        Unlike docker_sbx which uses a clone inside the microVM, OpenHands
        works directly in the ACP worktree. There's no remote to fetch.
        This method exists for interface compatibility.
        """
        return ""
