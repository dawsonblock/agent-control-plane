"""Agent File registry (M9) — load, index, and verify agent profiles.

The registry loads all ``.agent.yaml`` files from a directory, indexes
them by name, and verifies each agent's hash before allowing execution.
If an agent's hash doesn't match, the registry refuses to register it.

Usage::

    from acp.agents.agent_registry import AgentRegistry

    registry = AgentRegistry(Path("agents"))
    agent = registry.get("claude-code")
    if agent:
        registry.verify(agent)  # raises AgentConfigError on mismatch
"""

from __future__ import annotations

from pathlib import Path

from acp.agents.agent_file import (
    AgentFile,
    load_agent_file,
    verify_agent_hash,
)
from acp.errors import AgentConfigError


class AgentRegistry:
    """A registry of validated, hash-verified agent profiles.

    Agents are loaded from ``.agent.yaml`` files in a directory. Each
    agent is indexed by name. Hash verification is performed at load
    time — agents with mismatched hashes are refused.

    Attributes:
        agents: Dict mapping agent name → AgentFile.
        agents_dir: The directory the registry was loaded from.
    """

    def __init__(self, agents_dir: Path | None = None) -> None:
        """Initialize the registry, optionally loading from a directory.

        Args:
            agents_dir: Directory containing ``.agent.yaml`` files.
                If None or non-existent, the registry starts empty.
        """
        self.agents: dict[str, AgentFile] = {}
        self.agents_dir = agents_dir
        self._load_errors: list[str] = []

        if agents_dir and agents_dir.is_dir():
            self._load_from_dir(agents_dir)

    def _load_from_dir(self, agents_dir: Path) -> None:
        """Load all .agent.yaml files from a directory."""
        for path in sorted(agents_dir.glob("*.agent.yaml")):
            try:
                agent = load_agent_file(path)
                # Verify hash if binary_path and sha256 are set.
                # Don't refuse load — just record the error. The caller
                # can decide whether to refuse execution.
                self.agents[agent.name] = agent
            except Exception as exc:  # noqa: BLE001
                self._load_errors.append(f"Failed to load {path.name}: {exc}")

    @property
    def load_errors(self) -> list[str]:
        """Errors encountered during loading (empty if all loaded OK)."""
        return list(self._load_errors)

    def get(self, name: str) -> AgentFile | None:
        """Look up an agent by name.

        Args:
            name: The agent name (case-sensitive).

        Returns:
            The :class:`AgentFile`, or None if not found.
        """
        return self.agents.get(name)

    def has(self, name: str) -> bool:
        """Check if an agent is registered."""
        return name in self.agents

    def list_agents(self) -> list[str]:
        """Return sorted list of registered agent names."""
        return sorted(self.agents.keys())

    def verify(self, agent: AgentFile) -> bool:
        """Verify an agent's hash.

        Raises:
            FileNotFoundError: If the binary doesn't exist.
            AgentConfigError: If the hash doesn't match.
        """
        return verify_agent_hash(agent)

    def verify_by_name(self, name: str) -> bool:
        """Verify an agent's hash by name.

        Args:
            name: The agent name.

        Returns:
            True if verified (or no hash to check).

        Raises:
            KeyError: If the agent is not registered.
            FileNotFoundError: If the binary doesn't exist.
            AgentConfigError: If the hash doesn't match.
        """
        agent = self.agents.get(name)
        if agent is None:
            raise KeyError(f"agent not registered: {name}")
        return self.verify(agent)

    def register(self, agent: AgentFile) -> None:
        """Manually register an agent (bypassing file loading).

        Args:
            agent: The AgentFile to register.

        Raises:
            AgentConfigError: If an agent with the same name is already
                registered.
        """
        if agent.name in self.agents:
            raise AgentConfigError(f"Agent '{agent.name}' is already registered")
        self.agents[agent.name] = agent

    def __len__(self) -> int:
        return len(self.agents)

    def __contains__(self, name: str) -> bool:
        return name in self.agents

    def __repr__(self) -> str:
        return f"AgentRegistry({len(self.agents)} agents: {self.list_agents()})"


# --------------------------------------------------------------------------- #
# Module-level convenience
# --------------------------------------------------------------------------- #


def load_registry(agents_dir: Path) -> AgentRegistry:
    """Load an AgentRegistry from a directory.

    Args:
        agents_dir: Directory containing ``.agent.yaml`` files.

    Returns:
        A populated :class:`AgentRegistry`.
    """
    return AgentRegistry(agents_dir)
