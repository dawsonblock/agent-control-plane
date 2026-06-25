"""Execution backends for ACP.

The executor determines HOW the coding agent runs — in a git worktree
(default) or inside a Docker Sandbox microVM (``docker_sbx``). The agent
determines WHICH agent runs (Claude Code, Codex, etc.). These are
separate concerns: the executor provides the isolation boundary; the
agent provides the coding capability.

See:
  - ``worktree`` backend: traditional git worktree isolation (default)
  - ``docker_sbx`` backend: Docker Sandboxes (``sbx``) microVM isolation
"""

from __future__ import annotations

from acp.executor.sbx import SbxExecutor

__all__ = ["SbxExecutor"]
