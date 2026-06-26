"""Agent File (M9) — versioned, hashed, role-limited agent profiles.

An Agent File is a YAML manifest that describes a coding agent. It
contains the command template, capabilities, role, and a cryptographic
hash of the agent binary or script. The registry verifies the hash
before allowing execution — if the hash doesn't match, ACP refuses to
run the agent, preventing supply-chain attacks via malicious agent
binaries.

Schema (``agents/<name>.agent.yaml``)::

    name: claude-code
    version: "1.0.0"
    role: coder               # coder | reviewer | repair
    command_template: "claude-code --prompt-file {prompt_path}"
    capabilities:
      - code_edit
      - test_generation
      - file_read
    timeout_seconds: 1800
    max_repair_attempts: 5
    # SHA-256 of the agent binary or script. Verified at load time.
    # Generate with: sha256sum $(which claude-code)
    sha256: "a1b2c3d4..."
    # Optional: path to the binary/script to hash-verify.
    # If set, the registry checks the actual file's hash matches.
    binary_path: /usr/local/bin/claude-code
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from acp.errors import AgentConfigError


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VALID_ROLES = {"coder", "reviewer", "repair"}
REQUIRED_FIELDS = ("name", "version", "role", "command_template")


# --------------------------------------------------------------------------- #
# AgentFile model
# --------------------------------------------------------------------------- #


class AgentFile:
    """A validated agent profile loaded from a YAML manifest.

    Attributes:
        name: Unique agent identifier (e.g., "claude-code").
        version: Semantic version string.
        role: Agent role — "coder", "reviewer", or "repair".
        command_template: Shell command template with placeholders.
        capabilities: List of capability strings.
        timeout_seconds: Default timeout for this agent.
        max_repair_attempts: Default repair attempts for this agent.
        sha256: Expected SHA-256 hash of the agent binary/script.
        binary_path: Optional path to the binary to verify.
        source_path: Path to the YAML manifest file.
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        role: str,
        command_template: str,
        capabilities: list[str] | None = None,
        timeout_seconds: int = 1800,
        max_repair_attempts: int = 5,
        sha256: str = "",
        binary_path: Path | None = None,
        source_path: Path | None = None,
    ) -> None:
        if role not in VALID_ROLES:
            raise AgentConfigError(
                f"Agent role '{role}' is invalid. "
                f"Valid roles: {', '.join(sorted(VALID_ROLES))}."
            )
        self.name = name
        self.version = version
        self.role = role
        self.command_template = command_template
        self.capabilities = capabilities or []
        self.timeout_seconds = timeout_seconds
        self.max_repair_attempts = max_repair_attempts
        self.sha256 = sha256
        self.binary_path = binary_path
        self.source_path = source_path

    def __repr__(self) -> str:
        return (
            f"AgentFile(name={self.name!r}, version={self.version!r}, "
            f"role={self.role!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentFile):
            return NotImplemented
        return (
            self.name == other.name
            and self.version == other.version
            and self.role == other.role
            and self.command_template == other.command_template
            and self.sha256 == other.sha256
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to a dict (for YAML/JSON output)."""
        return {
            "name": self.name,
            "version": self.version,
            "role": self.role,
            "command_template": self.command_template,
            "capabilities": self.capabilities,
            "timeout_seconds": self.timeout_seconds,
            "max_repair_attempts": self.max_repair_attempts,
            "sha256": self.sha256,
            "binary_path": str(self.binary_path) if self.binary_path else None,
        }


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #


def load_agent_file(path: Path) -> AgentFile:
    """Load and validate an Agent File from a YAML manifest.

    Args:
        path: Path to a ``.agent.yaml`` file.

    Returns:
        The validated :class:`AgentFile`.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        AgentConfigError: If the file is malformed or validation fails.
    """
    if not path.is_file():
        raise FileNotFoundError(f"agent file not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise AgentConfigError(
            f"Failed to parse agent file {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise AgentConfigError(
            f"Agent file must be a YAML mapping: {path}"
        )

    return _validate_and_build(data, source_path=path)


def validate_agent_file_data(data: dict[str, Any]) -> AgentFile:
    """Validate agent file data (already parsed from YAML).

    Args:
        data: The parsed YAML dict.

    Returns:
        The validated :class:`AgentFile`.

    Raises:
        AgentConfigError: If validation fails.
    """
    return _validate_and_build(data, source_path=None)


def _validate_and_build(
    data: dict[str, Any],
    *,
    source_path: Path | None,
) -> AgentFile:
    """Internal: validate fields and build the AgentFile."""
    for field in REQUIRED_FIELDS:
        if field not in data:
            raise AgentConfigError(
                f"Agent file missing required field '{field}'"
            )

    name = data["name"]
    if not isinstance(name, str) or not name.strip():
        raise AgentConfigError("Agent 'name' must be a non-empty string")

    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise AgentConfigError("Agent 'version' must be a non-empty string")

    role = data["role"]
    if role not in VALID_ROLES:
        raise AgentConfigError(
            f"Agent role '{role}' is invalid. "
            f"Valid roles: {', '.join(sorted(VALID_ROLES))}."
        )

    command_template = data["command_template"]
    if not isinstance(command_template, str) or not command_template.strip():
        raise AgentConfigError(
            "Agent 'command_template' must be a non-empty string"
        )

    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list):
        raise AgentConfigError("Agent 'capabilities' must be a list")

    timeout = data.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or timeout <= 0:
        raise AgentConfigError("Agent 'timeout_seconds' must be a positive int")

    max_repair = data.get("max_repair_attempts", 5)
    if not isinstance(max_repair, int) or max_repair < 0:
        raise AgentConfigError(
            "Agent 'max_repair_attempts' must be a non-negative int"
        )

    sha256 = data.get("sha256", "")
    if not isinstance(sha256, str):
        raise AgentConfigError("Agent 'sha256' must be a string")

    binary_path = data.get("binary_path")
    if binary_path is not None:
        binary_path = Path(str(binary_path))

    return AgentFile(
        name=name,
        version=version,
        role=role,
        command_template=command_template,
        capabilities=capabilities,
        timeout_seconds=timeout,
        max_repair_attempts=max_repair,
        sha256=sha256,
        binary_path=binary_path,
        source_path=source_path,
    )


# --------------------------------------------------------------------------- #
# Hash verification
# --------------------------------------------------------------------------- #


def compute_file_hash(path: Path) -> str:
    """Compute the SHA-256 hash of a file.

    Args:
        path: Path to the file to hash.

    Returns:
        The hex-encoded SHA-256 digest.

    Raises:
        FileNotFoundError: If the file doesn't exist.
    """
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_agent_hash(agent: AgentFile) -> bool:
    """Verify that the agent's binary hash matches the expected hash.

    If the agent has no ``binary_path`` or no ``sha256``, verification
    is skipped (returns True — no hash to check).

    Args:
        agent: The agent file to verify.

    Returns:
        True if the hash matches (or no hash to check).

    Raises:
        FileNotFoundError: If ``binary_path`` is set but the file
            doesn't exist.
        AgentConfigError: If the hash doesn't match.
    """
    if not agent.binary_path or not agent.sha256:
        return True  # No hash to verify

    if not agent.binary_path.is_file():
        raise FileNotFoundError(
            f"Agent binary not found: {agent.binary_path}"
        )

    actual = compute_file_hash(agent.binary_path)
    if actual != agent.sha256:
        raise AgentConfigError(
            f"Agent '{agent.name}' hash mismatch! "
            f"Expected {agent.sha256[:16]}... but got {actual[:16]}.... "
            f"Refusing to execute — the agent binary may have been "
            f"tampered with or replaced."
        )

    return True
