"""Agent registry — the single source of truth for agent selection.

``build_agent(config)`` is the only place that decides which agent class to
instantiate. The control plane (CLI, graph nodes) never imports a concrete
agent class directly; it always goes through here. That's what makes agent
swapping a pure config change — the M2 acceptance gate.

v0.6.4 (M9): When ``config.agent.agents_dir`` is set, build_agent first
checks the Agent File registry. If the selected agent is registered, its
hash is verified before execution. A hash mismatch raises
``AgentConfigError`` — ACP refuses to run a tampered agent.
"""

from __future__ import annotations

from pathlib import Path

from acp.agents.agent_file import AgentFile
from acp.agents.agent_registry import AgentRegistry
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

    v0.6.4 (M9): If ``config.agent.agents_dir`` is set, the agent is
    looked up in the Agent File registry and its hash is verified
    before execution. A hash mismatch raises ``AgentConfigError``.

    Raises:
        AgentConfigError: For an unknown kind, a kind whose
            prerequisites aren't met, or a hash mismatch.
    """
    kind = config.agent.default.strip().lower()

    # v0.6.4 (M9): Verify agent hash if a registry is configured.
    agents_dir = getattr(config.agent, "agents_dir", None)
    if agents_dir:
        _verify_agent_from_registry(config, kind, agents_dir)

    factory = _AGENTS.get(kind)
    if factory is None:
        raise AgentConfigError(
            f"agent.default='{kind}' is not a known agent. "
            f"Known: {', '.join(sorted(_AGENTS))}."
        )
    return factory(config)


def _verify_agent_from_registry(
    config: RepoConfig,
    kind: str,
    agents_dir: Path,
) -> None:
    """Look up the agent in the registry and verify its hash.

    If the agent is not in the registry, this is a warning (not an
    error) — the agent may be a built-in (shell) that doesn't need
    a profile. If the agent IS in the registry but the hash doesn't
    match, this raises ``AgentConfigError`` (refuse to execute).
    """
    registry = AgentRegistry(agents_dir)
    agent_file = registry.get(kind)
    if agent_file is None:
        # Agent not in registry — could be a built-in. Allow but note.
        return

    # Verify the hash. Raises AgentConfigError on mismatch.
    registry.verify(agent_file)


def known_agents() -> list[str]:
    """Return the registered agent kind names (for docs / future CLI)."""
    return sorted(_AGENTS)


def get_agent_file(config: RepoConfig) -> AgentFile | None:
    """Get the AgentFile for the configured agent, if a registry is set.

    Returns None if no agents_dir is configured or the agent isn't
    in the registry.
    """
    agents_dir = getattr(config.agent, "agents_dir", None)
    if not agents_dir:
        return None

    registry = AgentRegistry(agents_dir)
    kind = config.agent.default.strip().lower()
    return registry.get(kind)
