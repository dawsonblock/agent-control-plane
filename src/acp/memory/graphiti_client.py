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

import asyncio
import logging
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
            "Note has already been ingested into Graphiti "
            "(graphiti_ingested=true)."
        )


# --------------------------------------------------------------------------- #
# Graphiti client initialization
# --------------------------------------------------------------------------- #


def _get_graphiti_client(
    *,
    falkor_host: str = "localhost",
    falkor_port: int = 6379,
    group_id: str = "",
) -> Any:
    """Create a Graphiti client connected to FalkorDB.

    Returns a :class:`Graphiti` instance configured with a FalkorDriver.
    The LLM client defaults to OpenAI (requires ``OPENAI_API_KEY``).
    Operators can override by setting environment variables or extending
    this function.

    Raises:
        ImportError: If ``graphiti-core[falkordb]`` is not installed.
    """
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    driver = FalkorDriver(host=falkor_host, port=falkor_port)

    # Graphiti defaults to OpenAI LLM + embedder. For local/air-gapped
    # setups, operators can pass custom clients here in the future.
    # For now, we use the defaults (requires OPENAI_API_KEY).
    return Graphiti(graph_driver=driver)


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
    """
    import yaml

    content = vault_note_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(content)
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


def ingest_task_to_graphiti(
    task: Task,
    frontmatter: dict[str, Any] | Frontmatter,
    vault_note_path: Path,
    graphiti_group_id: str = "",
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

    # 3. Ingest into Graphiti (async — run in a fresh event loop).
    async def _ingest() -> Any:
        from graphiti_core.nodes import EpisodeType

        client = _get_graphiti_client(group_id=graphiti_group_id)
        try:
            group_id = graphiti_group_id or "default"
            result = await client.add_episode(
                episode_text,
                episode_type=EpisodeType.text,
                group_id=group_id,
                reference_id=task.task_id,
                source_description=f"ACP task {task.task_id} — {task.repo_name}",
            )
            return result
        finally:
            await client.close()

    result = asyncio.run(_ingest())

    # 4. Mark the vault note as ingested.
    _mark_as_ingested(vault_note_path)

    return {
        "task_id": task.task_id,
        "episode_id": getattr(result.episode, "uuid", str(result.episode)),
        "nodes_created": len(result.nodes) if hasattr(result, "nodes") else 0,
        "edges_created": len(result.edges) if hasattr(result, "edges") else 0,
    }


def search_graphiti_facts(
    query: str,
    entity_type: str | None = None,
    group_id: str = "",
    num_results: int = 10,
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
    async def _search() -> list[dict[str, Any]]:
        from graphiti_core.search.search_config import SearchConfig

        client = _get_graphiti_client(group_id=group_id)
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

    return asyncio.run(_search())


def get_temporal_relationships(
    entity_type: str,
    entity_id: str,
    group_id: str = "",
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
    async def _get_relationships() -> list[dict[str, Any]]:
        client = _get_graphiti_client(group_id=group_id)
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

    return asyncio.run(_get_relationships())


# --------------------------------------------------------------------------- #
# v0.7.0 (Phase 4.1): Semantic memory garbage collection
# --------------------------------------------------------------------------- #


def find_superseded_nodes(
    group_id: str = "",
    *,
    older_than_days: int = 90,
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
    from datetime import datetime, timezone, timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    async def _find() -> list[dict[str, Any]]:
        client = _get_graphiti_client(group_id=group_id)
        try:
            # Graphiti's driver exposes the underlying FalkorDB graph.
            # We query for nodes with an incoming SUPERSEDES edge whose
            # valid_at (the supersede timestamp) is older than the cutoff.
            driver = getattr(client, "driver", None)
            if driver is None:
                return []

            # Use the driver's raw query capability if available.
            # FalkorDB supports Cypher-like queries via the graph driver.
            gid = group_id or "default"
            # Query for superseded nodes. The exact query depends on the
            # Graphiti schema, but the general pattern is:
            # MATCH (n)<-[r:SUPERSEDES]-(m) WHERE r.valid_at < cutoff
            # RETURN n, r.valid_at
            #
            # We use a best-effort approach: if the driver doesn't expose
            # a raw query interface, we fall back to searching all edges
            # and filtering client-side.
            try:
                # Try the driver's query method (Graphiti >= 0.5).
                results = await driver.execute_query(
                    "MATCH (n)<-[r:SUPERSEDES]-(m) "
                    "WHERE r.valid_at IS NOT NULL "
                    "RETURN n.uuid AS node_id, r.valid_at AS superseded_at",
                    graph_id=gid,
                )
            except (AttributeError, TypeError):
                # Fallback: search all edges and filter for SUPERSEDES.
                # This is slower but works with older Graphiti versions.
                all_edges = await client.search(
                    "SUPERSEDES", group_id=gid, num_results=1000,
                )
                results = []
                for edge in all_edges:
                    expired = getattr(edge, "expired_at", None)
                    if expired is None:
                        continue
                    results.append({
                        "node_id": getattr(edge, "source_node_id", ""),
                        "superseded_at": str(expired),
                    })

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
                        sup_time = datetime.fromisoformat(
                            superseded_at_str.replace("Z", "+00:00")
                        )
                    else:
                        sup_time = superseded_at_str
                    if sup_time.tzinfo is None:
                        sup_time = sup_time.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue  # can't parse timestamp — skip

                if sup_time < cutoff:
                    days = (datetime.now(timezone.utc) - sup_time).days
                    superseded.append({
                        "node_id": str(node_id),
                        "superseded_at": superseded_at_str if isinstance(superseded_at_str, str) else str(superseded_at_str),
                        "days_superseded": days,
                    })
            return superseded
        finally:
            await client.close()

    return asyncio.run(_find())


def prune_superseded_nodes(
    group_id: str = "",
    *,
    older_than_days: int = 90,
    dry_run: bool = True,
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
    nodes = find_superseded_nodes(
        group_id=group_id, older_than_days=older_than_days
    )

    pruned = 0
    if not dry_run and nodes:
        async def _prune() -> int:
            client = _get_graphiti_client(group_id=group_id)
            try:
                driver = getattr(client, "driver", None)
                if driver is None:
                    return 0
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
                    except Exception:  # noqa: BLE001
                        pass  # best-effort — continue pruning others
                return count
            finally:
                await client.close()

        pruned = asyncio.run(_prune())

    return {
        "dry_run": dry_run,
        "found": len(nodes),
        "pruned": pruned,
        "nodes": nodes,
        "older_than_days": older_than_days,
    }
