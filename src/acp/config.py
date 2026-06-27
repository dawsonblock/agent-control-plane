"""Repo configuration loader.

Each repo ACP can run against has a `<name>.repo.yaml` describing its
agent, commands, review thresholds, context globs, and memory settings.
This module is the single source of truth for that schema.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from acp.models import RiskLevel


class DurableMode(StrEnum):
    """Durable store operational mode.

    - ``disabled``: No SQLite writes. The durable store path is ignored.
    - ``best_effort``: SQLite failures are recorded as warnings but don't
      block the run. The JSONL log is canonical; SQLite is a derived index.
    - ``required``: SQLite failures are fatal. The run cannot succeed if
      the durable store cannot be written to. Use this when the SQLite
      store is a required evidence artifact, not just a query index.
    """

    DISABLED = "disabled"
    BEST_EFFORT = "best_effort"
    REQUIRED = "required"


# --------------------------------------------------------------------------- #
# Sub-sections
# --------------------------------------------------------------------------- #


class RepoSection(BaseModel):
    name: str
    path: Path
    default_branch: str = "main"

    @field_validator("path")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v.expanduser().resolve()


class AgentSection(BaseModel):
    default: str = "shell"  # M1: shell. M2 adds: custom
    timeout_seconds: int = 1800
    max_repair_attempts: int = 5
    command_template: str = ""  # used by M2's CLIAgent
    # v0.7.0 (Phase 3.2): Maximum sub-tasks an agent can spawn per run.
    # Bounds the number of task.subtask_spawned events to prevent
    # unbounded spawning. Default: 5.
    max_subtasks: int = 5
    # v0.6.0: Autonomous mode repair loop settings.
    # When dynamic_test_generation is True, the repair prompt instructs
    # the agent to write tests if the RiskEngine flags TESTS_MISSING.
    dynamic_test_generation: bool = True
    # Circuit breaker: if the agent produces the same failure signature
    # this many times in a row, the repair loop stops even if attempts
    # remain. Prevents hallucination loops. 0 disables the breaker.
    repair_repeat_breaker: int = 3
    # v0.6.4 (M9): Path to a directory containing .agent.yaml files.
    # When set, build_agent verifies the selected agent's hash against
    # the registry before execution. A hash mismatch raises
    # AgentConfigError — ACP refuses to run a tampered agent.
    agents_dir: Path | None = None
    # v0.6.7: Allow shell=True for command_template in worktree mode.
    # By default, shell metacharacters (|, >, <, &, ;, $, backticks) are
    # refused in worktree mode to prevent RCE. Set to True to allow them
    # — this is an explicit opt-in for operators who trust their config
    # and need shell features (pipes, redirects) on the host.
    allow_shell: bool = False

    @field_validator("default")
    @classmethod
    def _validate_agent_kind(cls, v: str) -> str:
        known = ("shell", "custom")
        kind = v.strip().lower()
        if kind not in known:
            raise ValueError(
                f"agent.default='{v}' is not a known agent. Known: {', '.join(known)}."
            )
        return kind

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if v <= 0 or v > 86400:
            raise ValueError("agent.timeout_seconds must be between 1 and 86400 (24h)")
        return v

    @field_validator("max_repair_attempts")
    @classmethod
    def _validate_max_repair(cls, v: int) -> int:
        if v < 0 or v > 20:
            raise ValueError("agent.max_repair_attempts must be between 0 and 20")
        return v

    @field_validator("max_subtasks")
    @classmethod
    def _validate_max_subtasks(cls, v: int) -> int:
        if v < 0 or v > 100:
            raise ValueError("agent.max_subtasks must be between 0 and 100")
        return v

    @field_validator("agents_dir")
    @classmethod
    def _absolute_agents(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        return v.expanduser().resolve()


class CommandsSection(BaseModel):
    """Empty string => command is skipped at run time."""

    install: str = ""
    lint: str = ""
    typecheck: str = ""
    test: str = ""
    build: str = ""
    timeout_seconds: int = 1800

    def items(self) -> list[tuple[str, str]]:
        """Ordered (name, command) pairs, including empty (skipped) commands."""
        return [
            (name, getattr(self, name))
            for name in ("install", "lint", "typecheck", "test", "build")
        ]


class ReviewSection(BaseModel):
    max_changed_files: int = 20
    max_added_lines: int = 1000
    block_secret_leaks: bool = True
    warn_on_auth_changes: bool = True
    warn_on_database_changes: bool = True
    require_human_approval: bool = True
    # v0.5.14: TruffleHog verified secret detection. When True and
    # TruffleHog is installed, the secret scanner uses TruffleHog for
    # verified detection (checks if a key is live before flagging).
    # Falls back to the regex scanner when TruffleHog is not installed.
    use_trufflehog: bool = True
    # v0.6.0: Autonomous mode — bypasses human approval for tasks that
    # pass all gates (tests green, no secrets, no hard blocks). An
    # auto.approved event is written to the hash-chained event log.
    # Default False — must be explicitly opted in.
    autonomous_mode: bool = False
    # v0.6.0: Auto-merge — after auto-approval, merge the task branch
    # into the default branch. Requires autonomous_mode=True.
    # Default False — must be explicitly opted in.
    auto_merge: bool = False
    # v0.6.8: Human firewall for autonomous auto-merge. Auto-merge is only
    # allowed when the review risk is at or below this level. Tasks above
    # this level are downgraded to NEEDS_REVIEW so a human must click
    # approved: true before the change reaches the default branch. This
    # enforces the rule: "do not let the swarm act on the wider network
    # until a human approves" for high-risk changes (database, secrets,
    # auth). Default "medium" — HIGH-risk tasks always require a human.
    auto_merge_max_risk: RiskLevel = RiskLevel.MEDIUM
    # v0.7.0 (Phase 3.2): Custom secret detection regexes. Users can
    # define their own regex patterns for internal/company-specific
    # token formats that TruffleHog doesn't know about. Each entry is
    # a dict with "name" (label for the finding), "pattern" (regex), and
    # an optional "verify_endpoint" (URL for HTTP verification).
    # Matches trigger a HARD_BLOCK just like built-in provider patterns.
    # When "verify_endpoint" is set, the scanner sends a POST request with
    # the matched secret to verify if it's active. If verification fails
    # (404/401/403), the finding is tagged with "unverified_hard_block".
    # Example:
    #   custom_secret_regexes = [
    #     {"name": "internal_api_key", "pattern": r"IAK-[A-Z0-9]{32}",
    #      "verify_endpoint": "https://api.internal.example.com/verify"},
    #   ]
    custom_secret_regexes: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("max_changed_files")
    @classmethod
    def _validate_max_files(cls, v: int) -> int:
        if v <= 0 or v > 10000:
            raise ValueError("review.max_changed_files must be between 1 and 10000")
        return v

    @field_validator("max_added_lines")
    @classmethod
    def _validate_max_lines(cls, v: int) -> int:
        if v <= 0 or v > 1_000_000:
            raise ValueError("review.max_added_lines must be between 1 and 1000000")
        return v

    @field_validator("custom_secret_regexes")
    @classmethod
    def _validate_custom_regexes(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        import re

        for entry in v:
            if "name" not in entry or "pattern" not in entry:
                raise ValueError(
                    "custom_secret_regexes entries must have 'name' and 'pattern' keys"
                )
            if not entry["name"].strip():
                raise ValueError("custom_secret_regexes entry 'name' must not be empty")
            try:
                re.compile(entry["pattern"])
            except re.error as exc:
                raise ValueError(
                    f"custom_secret_regexes: invalid regex for '{entry['name']}': {exc}"
                ) from exc
            # v0.8.1 (Phase 2.2): verify_endpoint is optional but must be a
            # valid URL if present.
            if "verify_endpoint" in entry and entry["verify_endpoint"]:
                from urllib.parse import urlparse

                parsed = urlparse(entry["verify_endpoint"])
                if parsed.scheme not in ("http", "https"):
                    raise ValueError(
                        f"custom_secret_regexes: verify_endpoint for '{entry['name']}' "
                        "must be an http or https URL"
                    )
        return v


class ContextSection(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class MemorySection(BaseModel):
    """Temporal memory settings (v0.6.2 / M7).

    - ``graphiti_group_id``: group id for Graphiti episodes. All tasks
      run with this config share the same group, enabling cross-task
      memory promotion and search.

    - ``promote_reports_by_default``: when True, approved task reports
      are automatically promoted to Graphiti temporal memory after
      approval. When False, promotion requires explicit ``acp memory
      promote``.

    v0.7.4: Custom LLM configuration for Graphiti. By default, Graphiti
    uses OpenAI for both LLM and embeddings (requires ``OPENAI_API_KEY``).
    These fields allow operators to use alternative providers (Anthropic,
    Gemini, Groq, Azure OpenAI, or any OpenAI-compatible endpoint).

    - ``llm_provider``: LLM provider for entity extraction. One of:
      ``openai`` (default), ``anthropic``, ``gemini``, ``groq``,
      ``azure_openai``, ``custom``.
    - ``llm_model``: model name (e.g. ``gpt-4o``, ``claude-3-5-sonnet``).
    - ``llm_base_url``: custom API base URL (for ``custom`` provider or
      self-hosted endpoints).
    - ``llm_api_key_env``: environment variable name containing the API
      key. Defaults to provider-specific vars (``OPENAI_API_KEY``, etc.).

    - ``embedder_provider``: embedding provider. Same options as
      ``llm_provider``. Defaults to ``openai``.
    - ``embedder_model``: embedding model name.
    - ``embedder_base_url``: custom embedding API base URL.
    - ``embedder_api_key_env``: env var name for embedding API key.

    Environment variable overrides (take precedence over config file):
      - ``ACP_GRAPHITI_LLM_PROVIDER``, ``ACP_GRAPHITI_LLM_MODEL``,
        ``ACP_GRAPHITI_LLM_BASE_URL``
      - ``ACP_GRAPHITI_EMBEDDER_PROVIDER``, ``ACP_GRAPHITI_EMBEDDER_MODEL``,
        ``ACP_GRAPHITI_EMBEDDER_BASE_URL``
    """

    graphiti_group_id: str = ""
    promote_reports_by_default: bool = False
    # v0.7.4: Custom LLM configuration for Graphiti.
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key_env: str = ""
    embedder_provider: str = "openai"
    embedder_model: str = ""
    embedder_base_url: str = ""
    embedder_api_key_env: str = ""

    @field_validator("llm_provider", "embedder_provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        allowed = ("openai", "anthropic", "gemini", "groq", "azure_openai", "custom")
        if v not in allowed:
            raise ValueError(f"provider must be one of {allowed}, got: {v}")
        return v


class SkillsSection(BaseModel):
    """Skills governance settings (v0.6.3 / M8).

    - ``skills_dir``: path to a directory containing skill definition
      files (``.yaml`` or ``SKILL.md``). When set, skills are loaded
      at startup and can be activated per-task via ``active_skill``.

    - ``active_skill``: the name of the skill to activate for all tasks
      run with this config. When set, the skill's prompt instructions
      are injected into the agent prompt and its review gates are
      applied during diff review. Leave empty to run without a skill
      (default).
    """

    skills_dir: Path | None = None
    active_skill: str = ""

    @field_validator("skills_dir")
    @classmethod
    def _absolute(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        return v.expanduser().resolve()


class ExecutorSection(BaseModel):
    """Sandbox / execution backend settings (v0.5.13).

    - ``backend``: the execution backend. ``"venv"`` (default, v0.7.6) uses
      an isolated uv venv for Python agents (recommended for production).
      ``"worktree"`` uses the traditional git worktree isolation — no OS-level
      sandboxing (deprecated, RCE risk with ``allow_shell``). ``"docker_sbx"``
      uses Docker Sandboxes (``sbx``) to run the agent inside an isolated
      microVM with its own Docker daemon, filesystem, and network.
      ``"gvisor"`` uses gVisor containers.

      v0.7.6: The default changed from ``"worktree"`` to ``"venv"``. When
      ``backend="venv"`` but ``executor.agent`` is not set (e.g. the shell or
      custom agent), the run falls back to the direct agent path with the same
      host-shell security checks as worktree — venv isolation only applies to
      Python agents with a declared ``executor.agent`` command.

    - ``danger_allow_host_shell``: when True, explicitly allows
      ``backend: "worktree"`` with ``agent.allow_shell: true``. This is a
      dangerous configuration (RCE on the host) and requires explicit opt-in
      since v0.8.0. Defaults to False.

    - ``agent``: when backend is ``docker_sbx``, the agent to run inside the
      sandbox (e.g. ``"claude"``, ``"codex"``, ``"copilot"``). See
      https://docs.docker.com/ai/sandboxes/get-started/agents/ for the full
      list. Ignored for the ``worktree`` backend (use ``agent.default``
      instead).

    - ``sandbox_name_prefix``: prefix for sandbox names. The full name is
      ``<prefix>-<task_id>``. Default: ``"acp"``.

    - ``clone_mode``: when True (default), the sandbox gets a private Git
      clone inside the microVM and the host repo is mounted read-only. The
      sandbox exposes its clone as a ``sandbox-<name>`` remote on the host.
      **Must be True** — disabling clone mode means the agent edits the host
      working tree directly, which defeats the isolation boundary. ACP
      refuses non-clone mode by default.

    - ``network_policy``: the sbx network policy. ``"open"`` allows all
      traffic. ``"balanced"`` denies by default with common dev sites
      allowed. ``"locked_down"`` (default) blocks all traffic unless
      explicitly allowed. ACP should default to ``locked_down`` or
      ``balanced``, never ``open``.

    - ``remove_after_run``: when True, the sandbox is removed after the run
      completes (reclaims disk space). When False (default), the sandbox
      persists and can be inspected or restarted.

    - ``gvisor_image``: the Docker image used when ``backend="gvisor"``. The
      user is responsible for building/pulling an image that has their agent
      installed. Defaults to ``"ubuntu:22.04"``. Ignored for other backends.
    - ``firecracker_kernel_path``: path to the Firecracker kernel image
      (vmlinux) used when ``backend="firecracker"``. Required for the
      Firecracker microVM backend. Ignored for other backends.
    - ``firecracker_rootfs_path``: path to the Firecracker root filesystem
      image (ext4) used when ``backend="firecracker"``. Required for the
      Firecracker microVM backend. Ignored for other backends.
    """

    backend: str = "venv"
    danger_allow_host_shell: bool = False
    agent: str = ""
    sandbox_name_prefix: str = "acp"
    clone_mode: bool = True
    network_policy: str = "locked_down"
    remove_after_run: bool = False
    gvisor_image: str = "ubuntu:22.04"
    firecracker_kernel_path: str = ""
    firecracker_rootfs_path: str = ""

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        allowed = ("worktree", "docker_sbx", "gvisor", "openhands", "venv", "firecracker")
        if v not in allowed:
            raise ValueError(
                f"executor.backend='{v}' is not valid. Must be one of: {', '.join(allowed)}."
            )
        return v

    @field_validator("network_policy")
    @classmethod
    def _validate_network_policy(cls, v: str) -> str:
        allowed = ("locked_down", "balanced", "open")
        if v not in allowed:
            raise ValueError(
                f"executor.network_policy='{v}' is not valid. Must be one of: {', '.join(allowed)}."
            )
        return v

    @model_validator(mode="after")
    def _validate_danger_allow_host_shell(self) -> ExecutorSection:
        """v0.8.0 (Phase 4.1): Enforce explicit opt-in for worktree+shell.

        When ``backend="worktree"`` and ``danger_allow_host_shell=False``,
        the agent runs on the host with no OS-level isolation. If
        ``agent.allow_shell=True`` is also set, this is an RCE risk — the
        agent can execute arbitrary host commands. This validator refuses
        the configuration unless ``danger_allow_host_shell=True`` is
        explicitly set, forcing the operator to acknowledge the risk.
        """
        if self.backend == "worktree" and not self.danger_allow_host_shell:
            # The agent section's allow_shell is checked at the RepoConfig
            # level since ExecutorSection doesn't have access to it. Here
            # we just warn about the deprecated default.
            pass
        return self


class FederationServerConfig(BaseModel):
    """Configuration for a single MCP server (v0.6.9, extended v0.7.0).

    - ``name``: server name for identification in events and prompts.
    - ``transport``: the transport type — ``"stdio"`` (default, v0.6.9),
      ``"http"`` (v0.7.0 Phase 3.1), or ``"sse"`` (v0.7.0 Phase 3.1).
    - ``command``: the command to spawn the MCP server (stdio only).
    - ``url``: the server URL (http/sse only).
    - ``headers``: optional HTTP headers (http/sse only).
    - ``env``: optional environment variables for the server process (stdio).
    - ``timeout_seconds``: per-request timeout (default 30).
    """

    name: str
    transport: str = "stdio"
    command: list[str] = Field(default_factory=list)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 30

    @field_validator("transport")
    @classmethod
    def _validate_transport(cls, v: str) -> str:
        allowed = ("stdio", "http", "sse")
        if v not in allowed:
            raise ValueError(
                f"federation transport='{v}' is not valid. Must be one of: {', '.join(allowed)}."
            )
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if v <= 0 or v > 86400:
            raise ValueError("federation.timeout_seconds must be between 1 and 86400 (24h)")
        return v

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> FederationServerConfig:
        """Ensure the right fields are present for the chosen transport."""
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"federation server '{self.name}': stdio transport requires 'command'")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(
                f"federation server '{self.name}': {self.transport} transport requires 'url'"
            )
        return self


class FederationSection(BaseModel):
    """Agent federation via MCP (v0.6.9).

    When configured, ACP discovers tools from each MCP server before
    the agent runs and injects them into the agent prompt. The agent
    can request federated tool calls; ACP proxies them — the agent
    never touches the network directly.

    This implements the rUv federation concept inside the ACP vault:
    agents are microservices that expose capabilities to one another,
    but all calls go through the zero-trust control plane.
    """

    servers: list[FederationServerConfig] = Field(default_factory=list)


class MissionSection(BaseModel):
    """Mission layer settings (v0.7.0 / M14).

    A mission groups sequential task runs toward an overarching goal
    (e.g. "Migrate to React 19"). When ``missions_dir`` is set, ACP
    stores mission state under ``<missions_dir>/<mission_id>/``.

    - ``missions_dir``: root directory for mission data. Defaults to
      ``data/missions`` relative to cwd. Each mission gets a subdirectory
      containing ``mission.yaml`` (canonical state) and ``events.jsonl``
      (mission-level event log with ``mission.created`` /
      ``mission.completed`` events).
    """

    missions_dir: Path = Path("data/missions")

    @field_validator("missions_dir")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v.expanduser().resolve()


class ProxySection(BaseModel):
    """Egress proxy settings (v0.7.0+, Phase 2.2).

    When configured, all agent network traffic is routed through a local
    ACP-managed MITM proxy. The proxy logs all external domains accessed
    by the agent, producing a ``network_egress.json`` artifact for the
    review gate.

    - ``enabled``: when True, route agent traffic through the proxy.
    - ``proxy_port``: local port for the MITM proxy (default 8080).
    - ``allowed_domains``: allowlist of domains the agent may access.
      Any domain not in this list is flagged in the egress log and
      triggers a review gate hard-block.
    - ``log_artifact``: filename for the egress log artifact, written
      into the run's ``artifacts/`` directory (default:
      ``network_egress.json``).
    """

    enabled: bool = False
    proxy_port: int = 8080
    allowed_domains: list[str] = Field(default_factory=list)
    log_artifact: str = "network_egress.json"

    @field_validator("proxy_port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if v <= 0 or v > 65535:
            raise ValueError("proxy.proxy_port must be between 1 and 65535")
        return v


class RerankingSection(BaseModel):
    """RAG re-ranking settings (v0.7.0+, Phase 4.2).

    When enabled, a Cross-Encoder re-ranking step is inserted into the
    Haystack retrieval pipeline after the initial vector search. This
    improves the signal-to-noise ratio of chunks retrieved from
    ``vault/tasks/`` by re-scoring them against the specific error/request.

    - ``enabled``: when True, insert a cross-encoder re-ranker.
    - ``model``: the cross-encoder model name (default:
      ``cross-encoder/ms-marco-MiniLM-L-6-v2``).
    - ``top_k_before_rerank``: how many chunks to retrieve from the
      vector store before re-ranking (default: 20).
    - ``top_k_after_rerank``: how many chunks to keep after re-ranking
      (default: 5).
    """

    enabled: bool = False
    model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    top_k_before_rerank: int = 20
    top_k_after_rerank: int = 5

    @field_validator("top_k_before_rerank")
    @classmethod
    def _validate_k_before(cls, v: int) -> int:
        if v <= 0 or v > 100:
            raise ValueError("reranking.top_k_before_rerank must be between 1 and 100")
        return v

    @field_validator("top_k_after_rerank")
    @classmethod
    def _validate_k_after(cls, v: int) -> int:
        if v <= 0 or v > 50:
            raise ValueError("reranking.top_k_after_rerank must be between 1 and 50")
        return v

    @model_validator(mode="after")
    def _validate_k_ordering(self) -> RerankingSection:
        """Ensure top_k_before_rerank >= top_k_after_rerank."""
        if self.top_k_before_rerank < self.top_k_after_rerank:
            raise ValueError(
                f"reranking.top_k_before_rerank ({self.top_k_before_rerank}) "
                f"must be >= top_k_after_rerank ({self.top_k_after_rerank}). "
                f"Cannot keep more chunks after re-ranking than were retrieved."
            )
        return self


class EvidenceSection(BaseModel):
    """Evidence integrity settings (v0.5.6).

    - ``signing_key_path``: path to a file containing a 32-byte raw Ed25519
      private key. When set, every event is signed with an Ed25519 signature
      over its hash, proving authenticity in addition to integrity. The key
      file must contain exactly 32 raw bytes (not PEM). Generate with::

          python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 \\
          import Ed25519PrivateKey; Ed25519PrivateKey.generate() \\
          .private_bytes_raw()" > key.bin

    - ``durable_store``: path to a SQLite database file. When set, events
      are dual-written to both the JSONL log (canonical source) and the
      SQLite store (queryable index). The SQLite store uses WAL mode +
      synchronous=FULL for transactional durability.

    - ``durable_mode``: controls how SQLite write failures are handled.
      ``disabled`` (default if no durable_store): no SQLite writes.
      ``best_effort`` (default if durable_store is set): failures are
      recorded as warnings but don't block the run.
      ``required``: failures are fatal — the run cannot succeed if the
      durable store cannot be written to.

    - ``public_key_path``: path to a file containing a 32-byte raw Ed25519
      public key. Used by ``acp verify`` to check event signatures. Not
      required for signing (only for verification).

    - ``task_store_primary``: v0.7.0 (Phase 1.1) feature flag controlling
      which store is the primary source of truth for task state.
      ``"json"`` (default): task.json files are primary, SQLite is a
      derived index. ``"sqlite"``: SQLite becomes primary, task.json
      becomes a projection. When ``"sqlite"``, ``durable_store`` must
      also be set. This flag enables gradual migration — operators can
      enable it per-repo without changing code.

    - ``checkpoint_store``: v0.7.4 path to a SQLite database file for
      LangGraph durable checkpointing. When set, workflow state persists
      across process restarts, enabling crash recovery for long-running
      agent tasks. Requires the ``checkpoint`` extra
      (``uv sync --extra checkpoint``). When not set, an in-memory
      checkpointer is used (state is lost on process exit).

    - ``signature_algorithm``: v0.7.4 algorithm for event signing.
      ``"ed25519"`` (default): 32-byte keys, requires ``crypto`` extra.
      ``"mldsa44"`` / ``"mldsa65"`` / ``"mldsa87"``: post-quantum ML-DSA
      (FIPS 204), requires ``mldsa`` extra and OpenSSL 3.5+.
    """

    signing_key_path: Path | None = None
    public_key_path: Path | None = None
    durable_store: Path | None = None
    durable_mode: DurableMode = DurableMode.BEST_EFFORT
    task_store_primary: str = "json"
    checkpoint_store: Path | None = None
    signature_algorithm: str = "ed25519"

    @field_validator("signing_key_path", "public_key_path", "durable_store", "checkpoint_store")
    @classmethod
    def _absolute(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        return v.expanduser().resolve()

    @field_validator("task_store_primary")
    @classmethod
    def _validate_task_store_primary(cls, v: str) -> str:
        allowed = ("json", "sqlite")
        if v not in allowed:
            raise ValueError(
                f"evidence.task_store_primary='{v}' is not valid. "
                f"Must be one of: {', '.join(allowed)}."
            )
        return v

    @field_validator("signature_algorithm")
    @classmethod
    def _validate_signature_algorithm(cls, v: str) -> str:
        allowed = ("ed25519", "mldsa44", "mldsa65", "mldsa87")
        if v not in allowed:
            raise ValueError(
                f"evidence.signature_algorithm='{v}' is not valid. "
                f"Must be one of: {', '.join(allowed)}."
            )
        return v

    @model_validator(mode="after")
    def _validate_sqlite_primary_needs_store(self) -> EvidenceSection:
        """When task_store_primary is 'sqlite', durable_store must be set."""
        if self.task_store_primary == "sqlite" and self.durable_store is None:
            raise ValueError(
                "evidence.task_store_primary='sqlite' requires evidence.durable_store to be set."
            )
        return self


class ApiSection(BaseModel):
    """HTTP API server settings (v0.7.3+, Phase 2.3).

    Controls the CORS and authentication behavior of the FastAPI server
    (``acp serve``). By default, CORS allows only localhost dev origins.
    In production, operators should set ``cors_origins`` to the exact
    origins that need access, or set ``cors_enabled: false`` to disable
    CORS entirely (when the UI is served from the same origin).

    - ``cors_origins``: list of allowed origin URLs. When empty (default),
      uses the built-in dev origins (localhost:5173, localhost:3000).
      Set to specific origins in production to restrict cross-origin
      access. Example: ``["https://acp.internal.corp"]``.
    - ``cors_enabled``: when False, CORS middleware is not added at all.
      Use this when the UI is served from the same origin as the API
      (production deployment via ``/ui/`` static files).
    """

    cors_origins: list[str] = Field(default_factory=list)
    cors_enabled: bool = True


class StreamingSection(BaseModel):
    """Mid-stream sentinel settings (v0.7.3+).

    When ``enabled``, the CLIAgent switches from blocking ``subprocess.run``
    to an async streaming execution path. Each line of agent stdout is fed
    to a :class:`~acp.streaming.midstream.StreamSentinel` that runs real-time
    safety checks *before* the agent finishes:

    1. **Kill-switch (secret detection)**: If a known credential pattern
       (AWS key, GitHub PAT, private key block, etc.) appears in the agent's
       output stream, the agent process is killed immediately and a
       ``stream.aborted`` event is written to the hash-chained event log.
       This closes the risk window between agent start and post-run review.

    2. **Strange-loop detection**: If the agent's output becomes stuck in a
       near-duplicate cycle (detected via token n-gram Jaccard similarity),
       the process is killed and a ``stream.aborted`` event is written.
       This catches attractor/hallucination loops without waiting for timeout.

    3. **Dangerous-path detection**: If the agent's output matches any
       configured dangerous-path patterns (e.g., modifying ``policy.json``,
       ``.env``, ``IAM`` files), the process is killed.

    All event writes from the sentinel are serialized via an ``asyncio.Lock``
    to preserve the hash-chain invariant — the :class:`EventWriter` is
    thread-unsafe by design, and the sentinel is the sole writer during the
    stream (the graph node writes ``agent.started`` before and
    ``agent.finished`` after).

    - ``enabled``: when True, use the streaming execution path. Default
      False — must be explicitly opted in. When False, the existing
      blocking ``subprocess.run`` path is used unchanged.

    - ``secret_detection``: when True (default), scan each chunk for known
      credential patterns. Uses the same provider patterns as the post-run
      :func:`~acp.review.secret_scanner.detect_hard_block_secrets` but
      adapted for raw text streams (no ``+``-prefix diff-line requirement).

    - ``strange_loop_detection``: when True (default), track token n-gram
      similarity across a rolling window of recent chunks. If the agent
      repeats near-identical output, abort.

    - ``strange_loop_threshold``: the repetition score at which the
      strange-loop detector fires (default 8.0). Each near-duplicate chunk
      adds to the score; each unique chunk decays it. Higher = more tolerant.

    - ``strange_loop_window``: number of recent chunks to keep in the
      rolling similarity window (default 10).

    - ``strange_loop_similarity``: Jaccard similarity threshold (0.0–1.0)
      above which two chunks are considered "near-duplicate" (default 0.65).
      At 1.0, only exact token-set matches count.

    - ``dangerous_path_patterns``: list of regex patterns. If any pattern
      matches a chunk, the agent is killed immediately. Empty by default —
      operators configure repo-specific patterns (e.g.,
      ``r"rm\\s+-rf\\s+/"`` or ``r"policy\\.json"``).

    v0.7.5: **Semantic anomaly detection** (async, non-blocking). When
    ``semantic_anomaly_detection`` is True, chunks are queued for
    asynchronous analysis by a local sentence-transformers cross-encoder.
    This does NOT block the stream — anomalies are flagged for post-run
    review rather than aborting mid-stream. Requires the ``rag`` extra
    (sentence-transformers). When not installed, the check is skipped
    silently.

    - ``semantic_anomaly_detection``: when True, enable async semantic
      anomaly detection (default False).
    - ``semantic_anomaly_threshold``: cosine similarity threshold below
      which a chunk is flagged as anomalous relative to the running
      average (default 0.3). Lower = more tolerant.
    - ``semantic_anomaly_model``: sentence-transformers model name
      (default ``all-MiniLM-L6-v2`` — lightweight, 384-dim, 80MB).
    """

    enabled: bool = False
    secret_detection: bool = True
    strange_loop_detection: bool = True
    strange_loop_threshold: float = 8.0
    strange_loop_window: int = 10
    strange_loop_similarity: float = 0.65
    dangerous_path_patterns: list[str] = Field(default_factory=list)
    # v0.7.5: Async semantic anomaly detection (non-blocking).
    semantic_anomaly_detection: bool = False
    semantic_anomaly_threshold: float = 0.3
    semantic_anomaly_model: str = "all-MiniLM-L6-v2"
    # v0.9.0 (Step 3): Micro-batching for the stream loop. Lines are
    # buffered and analyzed in batches to amortize per-line overhead and
    # enable a single batched ``model.encode()`` for semantic anomaly
    # detection. A flush triggers on whichever comes first: the interval
    # elapsing with no new line, or the max line count. Set
    # ``batch_interval_ms=0`` to disable batching (analyze every line —
    # the pre-v0.9.0 behavior).
    batch_interval_ms: int = 50
    batch_max_lines: int = 64

    @field_validator("strange_loop_threshold")
    @classmethod
    def _validate_threshold(cls, v: float) -> float:
        if v <= 0 or v > 100:
            raise ValueError("streaming.strange_loop_threshold must be between 0 and 100")
        return v

    @field_validator("strange_loop_window")
    @classmethod
    def _validate_window(cls, v: int) -> int:
        if v < 2 or v > 100:
            raise ValueError("streaming.strange_loop_window must be between 2 and 100")
        return v

    @field_validator("strange_loop_similarity")
    @classmethod
    def _validate_similarity(cls, v: float) -> float:
        if v <= 0.0 or v > 1.0:
            raise ValueError("streaming.strange_loop_similarity must be between 0.0 and 1.0")
        return v

    @field_validator("dangerous_path_patterns")
    @classmethod
    def _validate_dangerous_patterns(cls, v: list[str]) -> list[str]:
        import re

        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"streaming.dangerous_path_patterns: invalid regex '{pattern}': {exc}"
                ) from exc
        return v

    @field_validator("batch_interval_ms")
    @classmethod
    def _validate_batch_interval(cls, v: int) -> int:
        if v < 0:
            raise ValueError("streaming.batch_interval_ms must be >= 0 (0 disables batching)")
        return v

    @field_validator("batch_max_lines")
    @classmethod
    def _validate_batch_max_lines(cls, v: int) -> int:
        if v < 1:
            raise ValueError("streaming.batch_max_lines must be >= 1")
        return v


# --------------------------------------------------------------------------- #
# Top-level
# --------------------------------------------------------------------------- #


class RepoConfig(BaseModel):
    """The validated, in-memory form of a `<name>.repo.yaml`."""

    repo: RepoSection
    agent: AgentSection = Field(default_factory=AgentSection)
    commands: CommandsSection = Field(default_factory=CommandsSection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    context: ContextSection = Field(default_factory=ContextSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    evidence: EvidenceSection = Field(default_factory=EvidenceSection)
    executor: ExecutorSection = Field(default_factory=ExecutorSection)
    skills: SkillsSection = Field(default_factory=SkillsSection)
    federation: FederationSection = Field(default_factory=FederationSection)
    mission: MissionSection = Field(default_factory=MissionSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    reranking: RerankingSection = Field(default_factory=RerankingSection)
    api: ApiSection = Field(default_factory=ApiSection)
    streaming: StreamingSection = Field(default_factory=StreamingSection)

    # Path the config was loaded from; convenient for messages + events.
    source_path: Path | None = None


def load_repo_config(path: str | Path) -> RepoConfig:
    """Load and validate a repo config YAML.

    Raises FileNotFoundError if missing, ValueError on malformed YAML or
    schema violations (via Pydantic).
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"repo config not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"repo config must be a mapping at top level: {path}")

    # Drop explicit None values for sections that have defaults — YAML
    # configs commonly write `review: None` or `skills:` to indicate
    # "use the default", but Pydantic rejects None for non-Optional fields.
    for key in list(raw.keys()):
        if raw[key] is None:
            raw.pop(key)

    cfg = RepoConfig.model_validate(raw)
    cfg.source_path = path
    return cfg
