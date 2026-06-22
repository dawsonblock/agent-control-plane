"""Agent adapters for the control plane.

This package contains the different agent implementations that can be used
by the control plane to perform coding tasks. Each agent implements the
``AgentProtocol`` interface and is responsible for executing a task
within a worktree.
"""

from acp.agents.base import AgentProtocol, write_prompt, write_repair_prompt
from acp.agents.cli_agent import CLIAgent
from acp.agents.registry import build_agent
from acp.agents.shell_agent import ShellAgent

__all__ = [
    "AgentProtocol",
    "write_prompt",
    "write_repair_prompt",
    "CLIAgent",
    "ShellAgent",
    "build_agent",
]