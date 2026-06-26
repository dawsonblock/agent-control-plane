"""Temporal memory (Milestone 7 — Graphiti + FalkorDB).

Promotes *approved* vault notes (approved: true, memory_status: active,
graphiti_ingested: false) into a temporal knowledge graph of verified
facts. Graphiti tracks facts over time: if a later task supersedes an
earlier one, the graph creates a SUPERSEDES edge so the system knows
what is *currently* true.

Human firewall (non-negotiable):
    Only notes that a human has read and approved enter temporal memory.
    The system cannot gaslight itself because every fact it remembers
    was first verified by a human.

Requires the ``memory`` optional dependency group::

    uv sync --extra memory

And a running FalkorDB instance::

    docker run -p 6379:6379 falkordb/falkordb
"""

from acp.memory.graphiti_client import (
    HumanFirewallError,
    get_temporal_relationships,
    ingest_task_to_graphiti,
    search_graphiti_facts,
)
from acp.memory.promotion_rules import (
    PROMOTION_BLOCKED,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_URGENT,
    get_promotion_exclusions,
    get_promotion_metadata,
    get_promotion_priority,
    should_promote_to_graphiti,
)

__all__ = [
    "HumanFirewallError",
    "ingest_task_to_graphiti",
    "search_graphiti_facts",
    "get_temporal_relationships",
    "should_promote_to_graphiti",
    "get_promotion_priority",
    "get_promotion_exclusions",
    "get_promotion_metadata",
    "PROMOTION_BLOCKED",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "PRIORITY_HIGH",
    "PRIORITY_URGENT",
]
