"""Cognitive memory tiers (v0.6.9) — SAFLA-inspired three-tier memory.

Unifies ACP's existing memory systems into a single cognitive memory
model with three tiers, mirroring the SAFLA approach from the rUv
ecosystem:

  1. **Working Memory** — the agent's context window. This is the
     context_bundle.md (Haystack RAG retrieval) that gets prepended to
     the agent prompt. Short-lived, per-task, in the prompt.

  2. **Episodic Memory** — what happened in this and previous sessions.
     This is the hash-chained events.jsonl log, indexed across all runs
     for cross-run recall. An agent can ask "what went wrong last time
     we touched auth.py?" and get answers from past episodes.

  3. **Semantic Memory** — long-term codebase facts. This is the
     Graphiti/FalkorDB temporal knowledge graph, populated only from
     human-approved vault notes. Facts like "the auth module uses
     OAuth2 with PKCE" persist across runs and are superseded when
     newer facts arrive.

The key insight: these tiers already existed in ACP as separate
systems (context_builder, events.py, graphiti_client). This module
unifies them under a single interface so the context builder can pull
from all three tiers when assembling the agent's working memory.

Human firewall (non-negotiable):
  - Working memory: anything from the repo (it's public code).
  - Episodic memory: past run events (they're tamper-evident logs).
  - Semantic memory: ONLY human-approved facts (Graphiti already
    enforces this via _check_human_firewall).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


@dataclass
class MemoryItem:
    """A single item retrieved from any memory tier."""

    tier: str  # "working" | "episodic" | "semantic"
    source: str  # e.g. "context_bundle", "events.jsonl", "graphiti"
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryBundle:
    """Aggregated memory from all three tiers.

    This is what gets injected into the agent's working memory (the
    prompt). The ``to_prompt_section`` method formats it as Markdown.
    """

    working: list[MemoryItem] = field(default_factory=list)
    episodic: list[MemoryItem] = field(default_factory=list)
    semantic: list[MemoryItem] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return len(self.working) + len(self.episodic) + len(self.semantic)

    def to_prompt_section(self) -> str:
        """Format the memory bundle as a Markdown section for the prompt."""
        if self.total_items == 0:
            return ""
        sections: list[str] = ["\n\nCognitive memory context:"]

        if self.working:
            sections.append("\n  [Working Memory] (retrieved from repo)")
            for item in self.working:
                sections.append(f"    - {item.content}")

        if self.episodic:
            sections.append("\n  [Episodic Memory] (past run experiences)")
            for item in self.episodic:
                task_id = item.metadata.get("task_id", "unknown")
                sections.append(f"    - ({task_id}) {item.content}")

        if self.semantic:
            sections.append("\n  [Semantic Memory] (long-term codebase facts)")
            for item in self.semantic:
                sections.append(f"    - {item.content}")

        return "\n".join(sections) + "\n"


# --------------------------------------------------------------------------- #
# Episodic memory store — cross-run event recall
# --------------------------------------------------------------------------- #


class EpisodicMemoryStore:
    """Indexes events.jsonl across all runs for cross-run recall.

    Episodic memory is the "what happened in previous sessions" tier.
    It scans the runs directory for past event logs and retrieves
    relevant episodes based on a query (e.g. "auth failures", "database
    migrations").

    The store is read-only — it never modifies event logs. It reads
    the hash-chained events.jsonl files, which are tamper-evident.
    """

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = Path(runs_root)

    def recall(
        self,
        query: str,
        max_episodes: int = 5,
    ) -> list[MemoryItem]:
        """Recall past episodes relevant to the query.

        Searches event payloads (task descriptions, error messages,
        review concerns) for the query string (case-insensitive).
        Returns up to ``max_episodes`` items, most recent first.

        This is a simple keyword search — not vector retrieval. It's
        intentionally lightweight: episodic memory is a complement to
        semantic memory (Graphiti), not a replacement.
        """
        if not self.runs_root.is_dir():
            return []

        query_lower = query.lower()
        episodes: list[MemoryItem] = []

        # Scan run directories (sorted newest-first by name).
        run_dirs = sorted(
            (d for d in self.runs_root.iterdir() if d.is_dir() and (d / "events.jsonl").is_file()),
            reverse=True,
        )

        for run_dir in run_dirs:
            if len(episodes) >= max_episodes:
                break
            task_id = run_dir.name
            events_path = run_dir / "events.jsonl"
            try:
                for line in events_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    import json

                    evt = json.loads(line)
                    # Search in event type + payload fields.
                    searchable = json.dumps(evt).lower()
                    if query_lower in searchable:
                        # Extract a human-readable summary.
                        evt_type = evt.get("type", "unknown")
                        payload = evt.get("payload", {})
                        summary = self._summarize_event(evt_type, payload, task_id)
                        episodes.append(
                            MemoryItem(
                                tier="episodic",
                                source="events.jsonl",
                                content=summary,
                                metadata={"task_id": task_id, "event_type": evt_type},
                            )
                        )
                        break  # one episode per run
            except Exception:  # noqa: BLE001
                continue  # malformed log — skip

        return episodes[:max_episodes]

    @staticmethod
    def _summarize_event(
        evt_type: str,
        payload: dict[str, Any],
        task_id: str,
    ) -> str:
        """Create a human-readable summary of an event."""
        if evt_type == "task.created":
            return f"Task started: {payload.get('user_request', '?')[:80]}"
        if evt_type == "task.failed":
            return f"Task failed: {payload.get('error', '?')[:80]}"
        if evt_type == "review.completed":
            risk = payload.get("risk", "?")
            rec = payload.get("recommendation", "?")
            return f"Review: risk={risk}, recommendation={rec}"
        if evt_type == "auto.merge.refused":
            return f"Auto-merge refused: {payload.get('reason', '?')}"
        if evt_type == "node.failed":
            return f"Node failed: {payload.get('node', '?')} — {payload.get('message', '?')[:60]}"
        return f"Event: {evt_type}"


# --------------------------------------------------------------------------- #
# Unified memory retriever — pulls from all three tiers
# --------------------------------------------------------------------------- #


class CognitiveMemoryRetriever:
    """Unifies all three memory tiers into a single retrieval interface.

    This is the "combine the two" piece: it takes the rUv/SAFLA
    three-tier cognitive memory model and implements it using ACP's
    existing infrastructure (Haystack for working, events.jsonl for
    episodic, Graphiti for semantic).

    Usage::

        retriever = CognitiveMemoryRetriever(
            runs_root=runs_root,
            vault_root=vault_root,
        )
        bundle = retriever.retrieve("add OAuth to auth module")
        prompt_section = bundle.to_prompt_section()
    """

    def __init__(
        self,
        runs_root: Path,
        vault_root: Path | None = None,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.vault_root = Path(vault_root) if vault_root else None
        self._episodic = EpisodicMemoryStore(self.runs_root)

    def retrieve(
        self,
        query: str,
        max_working: int = 5,
        max_episodic: int = 5,
        max_semantic: int = 5,
    ) -> MemoryBundle:
        """Retrieve memory items from all three tiers.

        - **Working**: uses Haystack RAG if available (graceful
          fallback to empty if the ``rag`` extra isn't installed).
        - **Episodic**: searches past event logs for the query.
        - **Semantic**: searches Graphiti if available (graceful
          fallback to empty if the ``memory`` extra isn't installed or
          FalkorDB isn't running).
        """
        bundle = MemoryBundle()

        # Working memory — Haystack RAG (optional).
        bundle.working = self._retrieve_working(query, max_working)

        # Episodic memory — events.jsonl cross-run search.
        bundle.episodic = self._episodic.recall(query, max_episodic)

        # Semantic memory — Graphiti (optional).
        bundle.semantic = self._retrieve_semantic(query, max_semantic)

        return bundle

    def _retrieve_working(
        self,
        query: str,
        max_items: int,
    ) -> list[MemoryItem]:
        """Retrieve working memory items via Haystack RAG."""
        try:
            from acp.config import ContextSection
            from acp.context.context_builder import ContextBuilder

            if self.vault_root is None:
                return []
            builder = ContextBuilder(
                repo_path=self.runs_root.parent,  # best-effort
                vault_root=self.vault_root,
                context_config=ContextSection(),
            )
            docs = builder.get_relevant_docs(query, top_k=max_items)
            return [
                MemoryItem(
                    tier="working",
                    source="haystack_rag",
                    content=doc.get("content", "")[:200],
                    metadata={"score": doc.get("score", 0)},
                )
                for doc in docs
            ]
        except ImportError:
            return []
        except Exception:  # noqa: BLE001
            logger.debug("Working memory retrieval failed", exc_info=True)
            return []

    def _retrieve_semantic(
        self,
        query: str,
        max_items: int,
    ) -> list[MemoryItem]:
        """Retrieve semantic memory items via Graphiti."""
        try:
            from acp.memory.graphiti_client import search_graphiti_facts

            facts = search_graphiti_facts(query, num_results=max_items)
            return [
                MemoryItem(
                    tier="semantic",
                    source="graphiti",
                    content=fact.get("fact", ""),
                    metadata={
                        "source_node": fact.get("source_node", ""),
                        "target_node": fact.get("target_node", ""),
                        "valid_at": fact.get("valid_at", ""),
                    },
                )
                for fact in facts
            ]
        except ImportError:
            return []
        except Exception:  # noqa: BLE001
            logger.debug("Semantic memory retrieval failed", exc_info=True)
            return []
