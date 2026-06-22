"""Agent registry — the single source of truth for agent selection.

``build_agent(config)`` is the only place that decides which agent class to
instantiate. The control plane (CLI, graph nodes) never imports a concrete
agent class directly; it always goes through here. That's what makes agent
swapping a pure config change — the M2 acceptance gate.
"""

from __future__ import annotations

from acp.agents.base import AgentProtocol
from acp.agents.cli_agent import CLIAgent
from acp.agents.shell_agent import ShellAgent
from acp.config import RepoConfig
from acp.errors import AgentConfigError

# Known agent kinds, keyed by config string. Lowercased on lookup.
_AGENTS = {
    "shell": lambda cfg: ShellAgent(),
    "custom": lambda cfg: CLIAgent(cfg),
}


def build_agent(config: RepoConfig) -> AgentProtocol:
    """Return the agent the repo config selected.

    Raises ``AgentConfigError`` for an unknown kind or a kind whose
    prerequisites aren't met (e.g. ``custom`` with an empty template).
    """
    kind = config.agent.default.strip().lower()
    factory = _AGENTS.get(kind)
    if factory is None:
        raise AgentConfigError(
            f"agent.default='{kind}' is not a known agent. "
            f"Known: {', '.join(sorted(_AGENTS))}."
        )
    return factory(config)


def known_agents() -> list[str]:
    """Return the registered agent kind names (for docs / future CLI)."""
    return sorted(_AGENTS)
