"""Executor protocol — pluggable execution backends (Phase 2.1).

The executor determines HOW the coding agent runs — the isolation
boundary and runtime environment. The agent determines WHICH agent
runs (Claude Code, Codex, OpenHands, etc.). These are separate concerns:

  - The executor provides the isolation boundary (worktree, sandbox,
    container, microVM).
  - The agent provides the coding capability.

v0.5.13: ``worktree`` (default) and ``docker_sbx`` backends.
v0.7.0 (Phase 2.1): ``openhands`` backend — runs the OpenHands agent
in headless mode inside its own Docker-based runtime.
v0.7.1: ``gvisor`` backend — gVisor (runsc) syscall-sandboxed containers.
v0.7.2: ``venv`` backend — hermetic Python isolation via ``uv run --isolated``.

The :class:`Executor` protocol is extracted so that new backends
(Firecracker microVMs, seccomp profiles, etc.) can be added without
modifying the workflow graph.

Executor interface:

    class Executor(Protocol):
        def start(self, *, task_id, prompt_path, repo_path,
                  artifact_dir, timeout_seconds) -> AgentResult: ...
        def stop(self) -> bool: ...
        def cleanup(self) -> None: ...
        @property
        def backend_name(self) -> str: ...

The ``start()`` method is the main entry point — it launches the
agent, waits for it to finish, and returns an :class:`AgentResult`.
The executor is responsible for writing stdout/stderr to the artifact
directory and returning the result with the correct paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from acp.models import AgentResult


@runtime_checkable
class Executor(Protocol):
    """Abstract executor interface for agent execution backends.

    All executors must implement:
      - ``start()``: launch the agent, wait for completion, return result
      - ``stop()``: stop the running execution (if applicable)
      - ``cleanup()``: clean up resources after the run
      - ``backend_name``: the backend identifier string
    """

    @property
    def backend_name(self) -> str:
        """The backend identifier (e.g. 'docker_sbx', 'openhands')."""
        ...

    async def start(
        self,
        *,
        task_id: str,
        prompt_path: Path,
        repo_path: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> AgentResult:
        """Launch the agent and wait for completion.

        Writes stdout/stderr to the artifact directory and returns an
        :class:`AgentResult` with the correct paths.
        """
        ...

    def stop(self) -> bool:
        """Stop the running execution. Returns True if successful."""
        ...

    def cleanup(self) -> None:
        """Clean up resources (containers, sandboxes, etc.)."""
        ...
