"""Temporal memory (Milestone 7 — Graphiti + FalkorDB). Reserved.

Promotes *approved* vault notes (approved: true, memory_status: active,
graphiti_ingested: false) into a temporal knowledge graph of verified
facts. Not built until the Obsidian review surface is trustworthy.
"""

from acp.memory.graphiti_client import (
    ingest_task_to_graphiti,
    search_graphiti_facts,
    get_temporal_relationships,
)
from acp.memory.promotion_rules import (
    should_promote_to_graphiti,
    get_promotion_priority,
    get_promotion_exclusions,
    get_promotion_metadata,
)

__all__ = [
    "ingest_task_to_graphiti",
    "search_graphiti_facts",
    "get_temporal_relationships",
    "should_promote_to_graphiti",
    "get_promotion_priority",
    "get_promotion_exclusions",
    "get_promotion_metadata",
]
