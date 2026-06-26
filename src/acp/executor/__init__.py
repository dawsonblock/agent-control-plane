"""Execution backends for ACP.

The executor determines HOW the coding agent runs — in a git worktree
(default), inside a Docker Sandbox microVM (``docker_sbx``), via
OpenHands' Docker runtime (``openhands``), or inside a gVisor-sandboxed
container (``gvisor``). The agent determines WHICH agent runs (Claude
Code, Codex, etc.). These are separate concerns: the executor provides
the isolation boundary; the agent provides the coding capability.

v0.5.13: ``worktree`` (default) and ``docker_sbx`` backends.
v0.7.0 (Phase 2.1): ``openhands`` backend + :class:`Executor` protocol.
v0.7.1 (Phase 3.1): ``gvisor`` backend — gVisor (runsc) OS-level sandbox.

See:
  - ``worktree`` backend: traditional git worktree isolation (default)
  - ``docker_sbx`` backend: Docker Sandboxes (``sbx``) microVM isolation
  - ``openhands`` backend: OpenHands headless mode with Docker runtime
  - ``gvisor`` backend: gVisor (runsc) syscall-level container isolation
"""

from __future__ import annotations

from acp.executor.protocol import Executor
from acp.executor.sbx import SbxExecutor
from acp.executor.openhands import OpenHandsExecutor
from acp.executor.gvisor import GvisorExecutor

__all__ = ["Executor", "SbxExecutor", "OpenHandsExecutor", "GvisorExecutor"]
