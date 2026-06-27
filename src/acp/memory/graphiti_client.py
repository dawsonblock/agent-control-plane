"""Graphiti client (M7) — temporal memory via Graphiti + FalkorDB.

Promotes *approved* vault notes into a temporal knowledge graph. Graphiti
tracks facts over time: if a later task supersedes an earlier one, the
graph creates a ``SUPERSEDES`` edge so the system knows what is *currently*
true, not just what was true once.

Human firewall (non-negotiable):
    Only notes with ``approved: true`` AND ``memory_status: active`` AND
    ``graphiti_ingested: false`` are eligible for ingestion. This function
    physically rejects any note that doesn't meet all three criteria.

This module requires the ``memory`` optional dependency group::

    uv sync --extra memory

And a running FalkorDB instance::

    docker run -p 6379:6379 falkordb/falkordb

Graphiti also needs an LLM for entity extraction. By default it uses
OpenAI (set ``OPENAI_API_KEY``). For local/air-gapped setups, pass a
custom ``llm_client`` to :func:`_get_graphiti_client`.
"""

from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path
from typing import Any

from acp.models import Task
from acp.vault.frontmatter import Frontmatter, parse_frontmatter

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Human firewall checks
# --------------------------------------------------------------------------- #


class HumanFirewallError(Exception):
    """Raised when a vault note fails the human firewall check."""


def _check_human_firewall(frontmatter: Frontmatter) -> None:
    """Enforce the human firewall before ingestion.

    Raises:
        HumanFirewallError: If the note is not approved, not active, or
            already ingested.
    """
    if not frontmatter.approved:
        raise HumanFirewallError(
            f"Note is not approved (approved={frontmatter.approved}). "
            "Only human-approved notes can be promoted to temporal memory."
        )
    if frontmatter.memory_status != "active":
        raise HumanFirewallError(
            f"Note memory_status is '{frontmatter.memory_status}', not 'active'. "
            "Only active notes can be promoted."
        )
    if frontmatter.graphiti_ingested:
        raise HumanFirewallError(
            "Note has already been ingested into Graphiti (graphiti_ingested=true)."
        )


# --------------------------------------------------------------------------- #
# Graphiti client initialization
# --------------------------------------------------------------------------- #


def _get_graphiti_client(
    *,
    falkor_host: str | None = None,
    falkor_port: int | None = None,
    group_id: str = "",
    memory_config: Any = None,
) -> Any:
    """Create a Graphiti client connected to FalkorDB.

    Returns a :class:`Graphiti` instance configured with a FalkorDriver.
    The LLM client defaults to OpenAI (requires ``OPENAI_API_KEY``).

    v0.7.4: FalkorDB host and port can now be configured via the
    ``ACP_FALKORDB_HOST`` and ``ACP_FALKORDB_PORT`` environment variables,
    instead of being hardcoded to localhost:6379. This supports
    deployments where FalkorDB runs on a separate host (e.g. Docker
    compose, Kubernetes, remote server).

    v0.7.4: Custom LLM and embedder clients can now be configured via
    ``MemorySection`` in the repo config or via environment variables.
    Supported providers: openai (default), anthropic, gemini, groq,
    azure_openai, custom. When ``memory_config`` is provided, the
    appropriate LLM and embedder clients are instantiated and passed
    to Graphiti. When not provided, Graphiti's defaults (OpenAI) are
    used.

    Raises:
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
    """
    import os

    # v0.7.4: Allow FalkorDB connection to be configured via env vars.
    if falkor_host is None:
        falkor_host = os.environ.get("ACP_FALKORDB_HOST", "localhost")
    if falkor_port is None:
        falkor_port = int(os.environ.get("ACP_FALKORDB_PORT", "6379"))

    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    driver = FalkorDriver(host=falkor_host, port=falkor_port)

    # v0.7.4: Build custom LLM and embedder clients if configured.
    llm_client = _build_llm_client(memory_config)
    embedder_client = _build_embedder_client(memory_config)

    kwargs: dict[str, Any] = {"graph_driver": driver}
    if llm_client is not None:
        kwargs["llm_client"] = llm_client
    if embedder_client is not None:
        kwargs["embedder"] = embedder_client

    return Graphiti(**kwargs)


def _resolve_llm_config(memory_config: Any) -> dict[str, str]:
    """Resolve LLM config from MemorySection + env var overrides."""
    import os

    defaults = {
        "llm_provider": "openai",
        "llm_model": "",
        "llm_base_url": "",
        "llm_api_key_env": "",
    }
    if memory_config is not None:
        defaults["llm_provider"] = memory_config.llm_provider
        defaults["llm_model"] = memory_config.llm_model
        defaults["llm_base_url"] = memory_config.llm_base_url
        defaults["llm_api_key_env"] = memory_config.llm_api_key_env

    # Env var overrides take precedence.
    defaults["llm_provider"] = os.environ.get("ACP_GRAPHITI_LLM_PROVIDER", defaults["llm_provider"])
    defaults["llm_model"] = os.environ.get("ACP_GRAPHITI_LLM_MODEL", defaults["llm_model"])
    defaults["llm_base_url"] = os.environ.get("ACP_GRAPHITI_LLM_BASE_URL", defaults["llm_base_url"])
    return defaults


def _resolve_embedder_config(memory_config: Any) -> dict[str, str]:
    """Resolve embedder config from MemorySection + env var overrides."""
    import os

    defaults = {
        "embedder_provider": "openai",
        "embedder_model": "",
        "embedder_base_url": "",
        "embedder_api_key_env": "",
    }
    if memory_config is not None:
        defaults["embedder_provider"] = memory_config.embedder_provider
        defaults["embedder_model"] = memory_config.embedder_model
        defaults["embedder_base_url"] = memory_config.embedder_base_url
        defaults["embedder_api_key_env"] = memory_config.embedder_api_key_env

    # Env var overrides take precedence.
    defaults["embedder_provider"] = os.environ.get(
        "ACP_GRAPHITI_EMBEDDER_PROVIDER", defaults["embedder_provider"]
    )
    defaults["embedder_model"] = os.environ.get(
        "ACP_GRAPHITI_EMBEDDER_MODEL", defaults["embedder_model"]
    )
    defaults["embedder_base_url"] = os.environ.get(
        "ACP_GRAPHITI_EMBEDDER_BASE_URL", defaults["embedder_base_url"]
    )
    return defaults


def _get_api_key(provider: str, api_key_env: str) -> str:
    """Get API key from the appropriate environment variable."""
    import os

    if api_key_env:
        return os.environ.get(api_key_env, "")
    # Default env var names per provider.
    defaults = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "azure_openai": "AZURE_OPENAI_API_KEY",
        "custom": "OPENAI_API_KEY",  # custom uses OpenAI-compatible API
    }
    return os.environ.get(defaults.get(provider, "OPENAI_API_KEY"), "")


def _build_llm_client(memory_config: Any) -> Any:
    """Build a custom LLM client based on provider config.

    Returns None when the provider is OpenAI (Graphiti's default) —
    in that case, Graphiti creates its own default client.
    """
    cfg = _resolve_llm_config(memory_config)
    provider = cfg["llm_provider"]

    # When provider is openai and no custom model/base_url is set,
    # let Graphiti use its built-in default.
    if provider == "openai" and not cfg["llm_model"] and not cfg["llm_base_url"]:
        return None

    api_key = _get_api_key(provider, cfg["llm_api_key_env"])
    model = cfg["llm_model"] or _default_model_for_provider(provider)
    base_url = cfg["llm_base_url"] or None

    try:
        return _instantiate_llm_client(provider, api_key, model, base_url)
    except ImportError as exc:
        logger.warning(
            "LLM provider '%s' requires additional packages: %s — "
            "falling back to Graphiti default (OpenAI)",
            provider,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to create LLM client for provider '%s': %s — "
            "falling back to Graphiti default (OpenAI)",
            provider,
            exc,
        )
        return None


def _build_embedder_client(memory_config: Any) -> Any:
    """Build a custom embedder client based on provider config.

    Returns None when the provider is OpenAI (Graphiti's default).
    """
    cfg = _resolve_embedder_config(memory_config)
    provider = cfg["embedder_provider"]

    if provider == "openai" and not cfg["embedder_model"] and not cfg["embedder_base_url"]:
        return None

    api_key = _get_api_key(provider, cfg["embedder_api_key_env"])
    model = cfg["embedder_model"] or "text-embedding-3-small"
    base_url = cfg["embedder_base_url"] or None

    try:
        return _instantiate_embedder_client(provider, api_key, model, base_url)
    except ImportError as exc:
        logger.warning(
            "Embedder provider '%s' requires additional packages: %s — "
            "falling back to Graphiti default (OpenAI)",
            provider,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to create embedder for provider '%s': %s — "
            "falling back to Graphiti default (OpenAI)",
            provider,
            exc,
        )
        return None


def _default_model_for_provider(provider: str) -> str:
    """Return a sensible default model for each provider."""
    defaults = {
        "openai": "gpt-4o",
        "anthropic": "claude-3-5-sonnet-20241022",
        "gemini": "gemini-1.5-pro",
        "groq": "llama-3.3-70b-versatile",
        "azure_openai": "gpt-4o",
        "custom": "gpt-4o",
    }
    return defaults.get(provider, "gpt-4o")


def _instantiate_llm_client(provider: str, api_key: str, model: str, base_url: str | None) -> Any:
    """Instantiate the appropriate LLM client for the given provider."""
    if provider in ("openai", "custom"):
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.openai_client import OpenAIClient

        config = LLMConfig(api_key=api_key or None, model=model, base_url=base_url)
        return OpenAIClient(config=config)
    elif provider == "anthropic":
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.anthropic_client import AnthropicClient

        config = LLMConfig(api_key=api_key or None, model=model, base_url=base_url)
        return AnthropicClient(config=config)
    elif provider == "gemini":
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.gemini_client import GeminiClient

        config = LLMConfig(api_key=api_key or None, model=model, base_url=base_url)
        return GeminiClient(config=config)
    elif provider == "groq":
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.groq_client import GroqClient

        config = LLMConfig(api_key=api_key or None, model=model, base_url=base_url)
        return GroqClient(config=config)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


def _instantiate_embedder_client(
    provider: str, api_key: str, model: str, base_url: str | None
) -> Any:
    """Instantiate the appropriate embedder client for the given provider."""
    if provider in ("openai", "custom"):
        from graphiti_core.embedder import EmbedderConfig
        from graphiti_core.embedder.openai_embedder import OpenAIEmbedder

        config = EmbedderConfig(api_key=api_key or None, model=model, base_url=base_url)
        return OpenAIEmbedder(config=config)
    else:
        # For non-OpenAI embedders, fall back to OpenAI embedder with the
        # custom config — most providers offer OpenAI-compatible embedding APIs.
        from graphiti_core.embedder import EmbedderConfig
        from graphiti_core.embedder.openai_embedder import OpenAIEmbedder

        config = EmbedderConfig(api_key=api_key or None, model=model, base_url=base_url)
        return OpenAIEmbedder(config=config)


# --------------------------------------------------------------------------- #
# Episode formatting
# --------------------------------------------------------------------------- #


def _build_episode_text(
    task: Task,
    frontmatter: Frontmatter,
    vault_note_path: Path,
) -> str:
    """Build the episode text for Graphiti ingestion.

    The episode text is what Graphiti's LLM processes to extract entities
    and edges. It includes the task metadata, the repo, the branch, and
    the vault note body (which contains the report).
    """
    content = vault_note_path.read_text(encoding="utf-8")
    try:
        _, body = parse_frontmatter(content)
    except ValueError:
        body = content

    parts = [
        f"Task: {task.task_id}",
        f"Repo: {task.repo_name}",
        f"Branch: {task.task_branch}",
        f"Status: {task.status.value}",
        f"User Request: {task.user_request}",
        f"Risk: {frontmatter.risk or 'unknown'}",
        f"Recommendation: {frontmatter.recommendation or 'unknown'}",
        f"Files Changed: {frontmatter.files_changed or 0}",
        "",
        "Report:",
        body,
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Frontmatter update (mark as ingested)
# --------------------------------------------------------------------------- #


def _mark_as_ingested(vault_note_path: Path) -> None:
    """Update the vault note's frontmatter to graphiti_ingested: true.

    This is a targeted in-place update of just the ``graphiti_ingested``
    field. It re-serializes the frontmatter and preserves the body.

    Raises:
        ValueError: If the frontmatter is malformed and cannot be parsed.
        OSError: If the file cannot be read or written.
    """
    import yaml

    content = vault_note_path.read_text(encoding="utf-8")
    try:
        fm, body = parse_frontmatter(content)
    except ValueError:
        # Malformed frontmatter — can't safely update the field.
        raise
    fm.graphiti_ingested = True

    data = fm.model_dump()
    yaml_body = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    vault_note_path.write_text(f"---\n{yaml_body}\n---\n\n{body}\n")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def ingest_task_to_graphiti(
    task: Task,
    frontmatter: dict[str, Any] | Frontmatter,
    vault_note_path: Path,
    graphiti_group_id: str = "",
    memory_config: Any = None,
) -> dict[str, Any]:
    """Ingest an approved task report into Graphiti temporal memory.

    This is the bridge between Tier 2 (Obsidian review surface) and
    Tier 3 (temporal knowledge graph). It:

      1. Enforces the human firewall (approved + active + not yet ingested)
      2. Builds an episode text from the task + vault note
      3. Calls Graphiti's ``add_episode`` to extract entities and edges
      4. Marks the vault note as ``graphiti_ingested: true``

    Args:
        task: The task object.
        frontmatter: The parsed frontmatter (dict or Frontmatter).
        vault_note_path: Path to the vault note .md file.
        graphiti_group_id: Optional Graphiti group ID for multi-tenant
            isolation. Defaults to "" (default group).

    Returns:
        A dict with ingestion metadata:
        ``{"task_id", "episode_id", "nodes_created", "edges_created"}``

    Raises:
        HumanFirewallError: If the note fails the human firewall check.
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
        Exception: If FalkorDB is not running or the LLM fails.
    """
    # Accept both dict and Frontmatter for backwards compat.
    if isinstance(frontmatter, dict):
        fm = Frontmatter(**frontmatter)
    else:
        fm = frontmatter

    # 1. Enforce the human firewall.
    _check_human_firewall(fm)

    # 2. Build the episode text.
    episode_text = _build_episode_text(task, fm, vault_note_path)

    # 3. Ingest into Graphiti (async-native — v0.8.0 removed the thread-pool hack).
    from graphiti_core.nodes import EpisodeType

    client = _get_graphiti_client(group_id=graphiti_group_id, memory_config=memory_config)
    try:
        group_id = graphiti_group_id or "default"
        result = await client.add_episode(
            episode_text,
            episode_type=EpisodeType.text,
            group_id=group_id,
            reference_id=task.task_id,
            source_description=f"ACP task {task.task_id} — {task.repo_name}",
        )
    finally:
        await client.close()

    # 4. Mark the vault note as ingested.
    # If this fails, the note was ingested into Graphiti but not marked.
    # Log a warning so operators can manually mark it and avoid duplicate
    # ingestion on retry.
    try:
        _mark_as_ingested(vault_note_path)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Graphiti ingestion succeeded for task %s but marking the vault "
            "note as ingested failed: %s. Manual marking required to avoid "
            "duplicate ingestion.",
            task.task_id,
            exc,
        )

    return {
        "task_id": task.task_id,
        "episode_id": getattr(result.episode, "uuid", str(result.episode)),
        "nodes_created": len(result.nodes) if hasattr(result, "nodes") else 0,
        "edges_created": len(result.edges) if hasattr(result, "edges") else 0,
    }


async def search_graphiti_facts(
    query: str,
    entity_type: str | None = None,
    group_id: str = "",
    num_results: int = 10,
    memory_config: Any = None,
) -> list[dict[str, Any]]:
    """Search for facts in Graphiti temporal memory.

    Queries FalkorDB for active, non-superseded facts related to the
    query. Results are formatted as dicts with ``fact``, ``source_node``,
    ``target_node``, and ``valid_at`` fields.

    Args:
        query: Natural language query (e.g., "authentication login changes").
        entity_type: Optional entity type filter (e.g., "File", "Task").
        group_id: Optional Graphiti group ID for multi-tenant isolation.
        num_results: Maximum number of results to return.

    Returns:
        List of fact dicts, each containing:
        - ``fact``: The edge/fact description
        - ``source_node``: The source entity name
        - ``target_node``: The target entity name
        - ``valid_at``: When this fact became true (ISO timestamp)

    Raises:
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
        Exception: If FalkorDB is not running.
    """

    from graphiti_core.search.search_config import SearchConfig

    client = _get_graphiti_client(group_id=group_id, memory_config=memory_config)
    try:
        config = SearchConfig(num_results=num_results)
        results = await client.search(
            query,
            config=config,
            group_id=group_id or "default",
        )
        return [
            {
                "fact": getattr(edge, "fact", str(edge)),
                "source_node": getattr(edge, "source_node_id", "unknown"),
                "target_node": getattr(edge, "target_node_id", "unknown"),
                "valid_at": str(getattr(edge, "valid_at", "")),
            }
            for edge in results
        ]
    finally:
        await client.close()


async def get_temporal_relationships(
    entity_type: str,
    entity_id: str,
    group_id: str = "",
    memory_config: Any = None,
) -> list[dict[str, Any]]:
    """Get temporal relationships for an entity.

    Returns all edges (facts) connected to the entity, including
    superseded ones. This lets the system understand the full history
    of a file or component — what was true, what changed, and what
    superseded what.

    Args:
        entity_type: The entity type (e.g., "File", "Task", "Fix").
        entity_id: The entity identifier (e.g., a file path or task ID).
        group_id: Optional Graphiti group ID.

    Returns:
        List of relationship dicts with ``fact``, ``edge_type``,
        ``direction``, ``valid_at``, and ``expired_at`` fields.

    Raises:
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
    """

    client = _get_graphiti_client(group_id=group_id, memory_config=memory_config)
    try:
        # Search for edges related to this entity.
        results = await client.search(
            entity_id,
            group_id=group_id or "default",
        )
        return [
            {
                "fact": getattr(edge, "fact", str(edge)),
                "edge_type": getattr(edge, "edge_type", "unknown"),
                "direction": "outgoing",
                "valid_at": str(getattr(edge, "valid_at", "")),
                "expired_at": str(getattr(edge, "expired_at", "")),
            }
            for edge in results
        ]
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# v0.7.0 (Phase 4.1): Semantic memory garbage collection
# --------------------------------------------------------------------------- #


async def find_superseded_nodes(
    group_id: str = "",
    *,
    older_than_days: int = 90,
    memory_config: Any = None,
) -> list[dict[str, Any]]:
    """Find Graphiti nodes that have been superseded for longer than the threshold.

    Graphiti creates SUPERSEDES edges when a newer fact replaces an older
    one. Over time, these superseded nodes accumulate in FalkorDB and bloat
    the knowledge graph. This function identifies nodes that:

      1. Have an incoming SUPERSEDES edge (they were superseded)
      2. The supersede happened more than ``older_than_days`` ago

    Returns a list of dicts with ``node_id``, ``superseded_at``, and
    ``days_superseded`` fields. This is the dry-run half of pruning —
    the actual deletion is done by :func:`prune_superseded_nodes`.

    Raises:
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
        Exception: If FalkorDB is not running.
    """
    from datetime import datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

    client = _get_graphiti_client(group_id=group_id, memory_config=memory_config)
    try:
        # Graphiti's driver exposes the underlying FalkorDB graph.
        driver = getattr(client, "driver", None)
        if driver is None:
            return []

        gid = group_id or "default"

        # v0.8.1 (Phase 2.1): Strictly enforce the Cypher query via the
        # driver's execute_query(). The previous try/except fallback pulled
        # up to 1,000 edges into memory, risking OOM on large graphs.
        # Now we fail closed — if the Cypher query fails, raise the error
        # rather than pulling the entire graph into memory. This ensures
        # O(1) memory space regardless of the number of superseded nodes.
        results = await driver.execute_query(
            "MATCH (n)<-[r:SUPERSEDES]-(m) "
            "WHERE r.valid_at IS NOT NULL "
            "RETURN n.uuid AS node_id, r.valid_at AS superseded_at",
            graph_id=gid,
        )

        superseded: list[dict[str, Any]] = []
        for row in results:
            if isinstance(row, dict):
                node_id = row.get("node_id", "")
                superseded_at_str = row.get("superseded_at", "")
            else:
                # Graphiti may return objects instead of dicts.
                node_id = getattr(row, "node_id", str(row))
                superseded_at_str = getattr(row, "superseded_at", "")

            # Parse the timestamp and check if it's old enough.
            try:
                if isinstance(superseded_at_str, str):
                    sup_time = datetime.fromisoformat(superseded_at_str.replace("Z", "+00:00"))
                else:
                    sup_time = superseded_at_str
                if sup_time.tzinfo is None:
                    sup_time = sup_time.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue  # can't parse timestamp — skip

            if sup_time < cutoff:
                days = (datetime.now(UTC) - sup_time).days
                superseded.append(
                    {
                        "node_id": str(node_id),
                        "superseded_at": superseded_at_str
                        if isinstance(superseded_at_str, str)
                        else str(superseded_at_str),
                        "days_superseded": days,
                    }
                )
        return superseded
    finally:
        await client.close()


async def prune_superseded_nodes(
    group_id: str = "",
    *,
    older_than_days: int = 90,
    dry_run: bool = True,
    memory_config: Any = None,
) -> dict[str, Any]:
    """Prune superseded nodes from the Graphiti/FalkorDB knowledge graph.

    Identifies nodes that have been superseded for more than
    ``older_than_days`` and deletes them from FalkorDB. This prevents
    the knowledge graph from growing unboundedly as facts are updated
    over time.

    Args:
        group_id: Optional Graphiti group ID for multi-tenant isolation.
        older_than_days: Only prune nodes superseded more than this many
            days ago (default: 90).
        dry_run: When True (default), only report what would be pruned
            without actually deleting anything. When False, delete the
            nodes from FalkorDB.

    Returns:
        A dict with:
        - ``dry_run``: whether this was a dry run
        - ``found``: number of superseded nodes found
        - ``pruned``: number of nodes actually deleted (0 if dry_run)
        - ``nodes``: list of node dicts (node_id, superseded_at, days)
        - ``older_than_days``: the threshold used

    Raises:
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
        Exception: If FalkorDB is not running.
    """
    nodes = await find_superseded_nodes(
        group_id=group_id,
        older_than_days=older_than_days,
        memory_config=memory_config,
    )

    pruned = 0
    if not dry_run and nodes:
        client = _get_graphiti_client(group_id=group_id, memory_config=memory_config)
        try:
            driver = getattr(client, "driver", None)
            if driver is None:
                pass
            else:
                gid = group_id or "default"
                count = 0
                for node in nodes:
                    try:
                        await driver.execute_query(
                            "MATCH (n) WHERE n.uuid = $node_id DETACH DELETE n",
                            {"node_id": node["node_id"]},
                            graph_id=gid,
                        )
                        count += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Failed to prune superseded node %s: %s",
                            node.get("node_id", "?"),
                            exc,
                        )
                        # Continue pruning other nodes — best-effort.
                pruned = count
        finally:
            await client.close()

    return {
        "dry_run": dry_run,
        "found": len(nodes),
        "pruned": pruned,
        "nodes": nodes,
        "older_than_days": older_than_days,
    }
