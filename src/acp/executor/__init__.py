"""Execution backends for ACP.

The executor determines HOW the coding agent runs — in a git worktree
(default), inside a Docker Sandbox microVM (``docker_sbx``), or via
OpenHands' Docker runtime (``openhands``). The agent determines WHICH
agent runs (Claude Code, Codex, etc.). These are separate concerns: the
executor provides the isolation boundary; the agent provides the coding
capability.

v0.5.13: ``worktree`` (default) and ``docker_sbx`` backends.
v0.7.0 (Phase 2.1): ``openhands`` backend + :class:`Executor` protocol.

See:
  - ``worktree`` backend: traditional git worktree isolation (default)
  - ``docker_sbx`` backend: Docker Sandboxes (``sbx``) microVM isolation
  - ``openhands`` backend: OpenHands headless mode with Docker runtime
"""

from __future__ import annotations

from acp.executor.protocol import Executor
from acp.executor.sbx import SbxExecutor
from acp.executor.openhands import OpenHandsExecutor

__all__ = ["Executor", "SbxExecutor", "OpenHandsExecutor"]
