"""Agent adapters for the control plane.

This package contains the different agent implementations that can be used
by the control plane to perform coding tasks. Each agent implements the
``AgentProtocol`` interface and is responsible for executing a task
within a worktree.

v0.6.4 (M9): Includes the Agent File registry — versioned, hashed,
role-limited agent profiles that prevent supply-chain attacks via
malicious agent binaries.
"""

from acp.agents.agent_file import (
    AgentFile,
    compute_file_hash,
    load_agent_file,
    validate_agent_file_data,
    verify_agent_hash,
)
from acp.agents.agent_registry import AgentRegistry, load_registry
from acp.agents.base import AgentProtocol, write_prompt, write_repair_prompt
from acp.agents.cli_agent import CLIAgent
from acp.agents.registry import build_agent, get_agent_file, known_agents
from acp.agents.shell_agent import ShellAgent

__all__ = [
    "AgentProtocol",
    "write_prompt",
    "write_repair_prompt",
    "CLIAgent",
    "ShellAgent",
    "build_agent",
    "known_agents",
    "get_agent_file",
    "AgentFile",
    "AgentRegistry",
    "load_registry",
    "load_agent_file",
    "validate_agent_file_data",
    "verify_agent_hash",
    "compute_file_hash",
]
