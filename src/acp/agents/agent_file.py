"""Agent File (M9) — versioned, hashed, role-limited agent profiles.

An Agent File is a YAML manifest that describes a coding agent. It
contains the command template, capabilities, role, and a cryptographic
hash of the agent binary or script. The registry verifies the hash
before allowing execution — if the hash doesn't match, ACP refuses to
run the agent, preventing supply-chain attacks via malicious agent
binaries.

v0.7.2 (Phase 1 — Hermetic Isolation): The schema now supports an
optional ``environment`` block that pins the agent's dependency tree.
When ``environment.manager`` is set (e.g., ``"uv"``), ACP verifies that
the lockfile hash (``dependencies_hash``) matches the actual lockfile
before execution. This prevents supply-chain attacks via hijacked Python
dependencies — even if the entrypoint binary hash matches, a modified
``uv.lock`` or ``requirements.txt`` would be caught.

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
    # v0.7.2: Hermetic environment isolation (optional).
    # When set, ACP verifies the dependency lockfile hash and runs the
    # agent in an isolated environment (uv run --isolated).
    environment:
      manager: "uv"              # uv | pip | none
      lockfile: "uv.lock"        # path relative to the agent's project dir
      dependencies_hash: "sha256:d3b07384d113edec49eaa6238ad5ff00"
      python_version: "3.12"     # required Python version
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from acp.errors import AgentConfigError

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VALID_ROLES = {"coder", "reviewer", "repair"}
REQUIRED_FIELDS = ("name", "version", "role", "command_template")
VALID_ENV_MANAGERS = {"uv", "pip", "none"}


# --------------------------------------------------------------------------- #
# Environment spec (v0.7.2 — Hermetic Isolation)
# --------------------------------------------------------------------------- #


@dataclass
class EnvironmentSpec:
    """Hermetic environment specification for an agent.

    When present, ACP verifies the dependency lockfile hash before
    execution and runs the agent in an isolated environment (e.g.,
    ``uv run --isolated``) to prevent supply-chain attacks via hijacked
    Python dependencies.

    Attributes:
        manager: The dependency manager — ``"uv"``, ``"pip"``, or ``"none"``.
        lockfile: Path to the lockfile (relative to the agent's project dir).
        dependencies_hash: Expected hash of the lockfile, prefixed with
            the algorithm (e.g., ``"sha256:abc123..."``).
        python_version: Required Python version string (e.g., ``"3.12"``).
    """

    manager: str = "none"
    lockfile: str = ""
    dependencies_hash: str = ""
    python_version: str = ""

    @property
    def is_isolated(self) -> bool:
        """True when this spec requests hermetic isolation."""
        return self.manager != "none"

    @property
    def hash_algorithm(self) -> str:
        """Extract the hash algorithm from dependencies_hash."""
        if ":" in self.dependencies_hash:
            return self.dependencies_hash.split(":", 1)[0]
        return "sha256"

    @property
    def hash_value(self) -> str:
        """Extract the hex digest from dependencies_hash."""
        if ":" in self.dependencies_hash:
            return self.dependencies_hash.split(":", 1)[1]
        return self.dependencies_hash


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
        environment: Optional hermetic environment spec (v0.7.2).
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
        environment: EnvironmentSpec | None = None,
        source_path: Path | None = None,
    ) -> None:
        if role not in VALID_ROLES:
            raise AgentConfigError(
                f"Agent role '{role}' is invalid. Valid roles: {', '.join(sorted(VALID_ROLES))}."
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
        self.environment = environment
        self.source_path = source_path

    def __repr__(self) -> str:
        return f"AgentFile(name={self.name!r}, version={self.version!r}, role={self.role!r})"

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
        d: dict[str, Any] = {
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
        if self.environment and self.environment.is_isolated:
            d["environment"] = {
                "manager": self.environment.manager,
                "lockfile": self.environment.lockfile,
                "dependencies_hash": self.environment.dependencies_hash,
                "python_version": self.environment.python_version,
            }
        return d


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
        raise AgentConfigError(f"Failed to parse agent file {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise AgentConfigError(f"Agent file must be a YAML mapping: {path}")

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
            raise AgentConfigError(f"Agent file missing required field '{field}'")

    name = data["name"]
    if not isinstance(name, str) or not name.strip():
        raise AgentConfigError("Agent 'name' must be a non-empty string")

    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise AgentConfigError("Agent 'version' must be a non-empty string")

    role = data["role"]
    if role not in VALID_ROLES:
        raise AgentConfigError(
            f"Agent role '{role}' is invalid. Valid roles: {', '.join(sorted(VALID_ROLES))}."
        )

    command_template = data["command_template"]
    if not isinstance(command_template, str) or not command_template.strip():
        raise AgentConfigError("Agent 'command_template' must be a non-empty string")

    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list):
        raise AgentConfigError("Agent 'capabilities' must be a list")

    timeout = data.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or timeout <= 0:
        raise AgentConfigError("Agent 'timeout_seconds' must be a positive int")

    max_repair = data.get("max_repair_attempts", 5)
    if not isinstance(max_repair, int) or max_repair < 0:
        raise AgentConfigError("Agent 'max_repair_attempts' must be a non-negative int")

    sha256 = data.get("sha256", "")
    if not isinstance(sha256, str):
        raise AgentConfigError("Agent 'sha256' must be a string")

    binary_path = data.get("binary_path")
    if binary_path is not None:
        binary_path = Path(str(binary_path))

    # v0.7.2: Parse optional environment block for hermetic isolation.
    environment = _parse_environment(data.get("environment"))

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
        environment=environment,
        source_path=source_path,
    )


def _parse_environment(data: Any) -> EnvironmentSpec | None:
    """Parse and validate the optional environment block.

    Returns None if no environment block is present. Returns an
    EnvironmentSpec with ``manager="none"`` if the block is present but
    empty. Raises AgentConfigError for invalid configurations.
    """
    if data is None:
        return None

    if not isinstance(data, dict):
        raise AgentConfigError("Agent 'environment' must be a YAML mapping")

    manager = data.get("manager", "none")
    if manager not in VALID_ENV_MANAGERS:
        raise AgentConfigError(
            f"environment.manager='{manager}' is invalid. "
            f"Valid values: {', '.join(sorted(VALID_ENV_MANAGERS))}."
        )

    lockfile = data.get("lockfile", "")
    if not isinstance(lockfile, str):
        raise AgentConfigError("environment.lockfile must be a string")

    dependencies_hash = data.get("dependencies_hash", "")
    if not isinstance(dependencies_hash, str):
        raise AgentConfigError("environment.dependencies_hash must be a string")

    python_version = data.get("python_version", "")
    if not isinstance(python_version, str):
        raise AgentConfigError("environment.python_version must be a string")

    # When manager is not "none", lockfile and dependencies_hash are required.
    if manager != "none":
        if not lockfile:
            raise AgentConfigError(
                "environment.lockfile is required when environment.manager is not 'none'"
            )
        if not dependencies_hash:
            raise AgentConfigError(
                "environment.dependencies_hash is required when environment.manager is not 'none'"
            )

    return EnvironmentSpec(
        manager=manager,
        lockfile=lockfile,
        dependencies_hash=dependencies_hash,
        python_version=python_version,
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
        raise FileNotFoundError(f"Agent binary not found: {agent.binary_path}")

    actual = compute_file_hash(agent.binary_path)
    if actual != agent.sha256:
        raise AgentConfigError(
            f"Agent '{agent.name}' hash mismatch! "
            f"Expected {agent.sha256[:16]}... but got {actual[:16]}.... "
            f"Refusing to execute — the agent binary may have been "
            f"tampered with or replaced."
        )

    return True


def verify_environment_hash(agent: AgentFile, project_dir: Path) -> bool:
    """Verify that the agent's dependency lockfile hash matches the expected hash.

    v0.7.2 (Phase 1 — Hermetic Isolation): If the agent has an
    :class:`EnvironmentSpec` with a ``dependencies_hash``, this function
    computes the actual hash of the lockfile and compares it. A mismatch
    means the dependency tree has been modified since the agent was
    registered — a potential supply-chain attack.

    Args:
        agent: The agent file to verify.
        project_dir: The agent's project directory (where the lockfile lives).

    Returns:
        True if the hash matches (or no environment hash to check).

    Raises:
        FileNotFoundError: If the lockfile doesn't exist.
        AgentConfigError: If the hash doesn't match.
    """
    if not agent.environment or not agent.environment.is_isolated:
        return True  # No environment to verify

    env = agent.environment
    lockfile_path = project_dir / env.lockfile
    if not lockfile_path.is_file():
        raise FileNotFoundError(f"Agent '{agent.name}' lockfile not found: {lockfile_path}")

    actual = compute_file_hash(lockfile_path)
    if actual != env.hash_value:
        raise AgentConfigError(
            f"Agent '{agent.name}' dependency hash mismatch! "
            f"Lockfile: {env.lockfile}\n"
            f"Expected {env.hash_value[:16]}... but got {actual[:16]}....\n"
            f"Refusing to execute — the dependency tree may have been "
            f"tampered with or replaced. Re-run agent registration to "
            f"update the hash, or restore the original lockfile."
        )

    return True
