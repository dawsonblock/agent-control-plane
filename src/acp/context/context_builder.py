"""Context builder (M6).

Uses Haystack V2 to retrieve relevant context for a coding task. Connects
a text embedder to an in-memory retriever, pulling from the index generated
by the :class:`HaystackIndexer`.

The output is a formatted Markdown string (``context_bundle.md``) that is
written to the artifacts directory and prepended to the agent's prompt.
Because it lands in ``artifacts/``, it is automatically hash-chained and
cryptographically signed by the evidence engine.

This module requires the ``rag`` optional dependency group::

    uv sync --extra rag
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from haystack import Pipeline
from haystack.components.embedders import SentenceTransformersTextEmbedder
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever

from acp.config import ContextSection
from acp.context.haystack_indexer import HaystackIndexer


class ContextBuilder:
    """Build context bundles for coding tasks using Haystack retrieval."""

    def __init__(
        self,
        repo_path: Path,
        vault_root: Path,
        context_config: ContextSection,
    ) -> None:
        self.indexer = HaystackIndexer(
            repo_path, vault_root, context_config,
        )

    def get_relevant_docs(
        self,
        task_description: str,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Run the query pipeline to fetch the most relevant text chunks."""
        # 1. Build the index if it hasn't been built yet
        if self.indexer.document_store.count_documents() == 0:
            self.indexer.build_index()

        # If the repository/vault is completely empty, short-circuit
        if self.indexer.document_store.count_documents() == 0:
            return []

        # 2. Build the query pipeline
        query_pipeline = Pipeline()

        # Embedder must use the exact same model as the indexer
        query_pipeline.add_component(
            "text_embedder",
            SentenceTransformersTextEmbedder(model="all-MiniLM-L6-v2"),
        )
        query_pipeline.add_component(
            "retriever",
            InMemoryEmbeddingRetriever(
                document_store=self.indexer.document_store, top_k=top_k,
            ),
        )

        query_pipeline.connect(
            "text_embedder.embedding", "retriever.query_embedding",
        )

        # 3. Execute the search
        result = query_pipeline.run(
            {"text_embedder": {"text": task_description}},
        )
        docs = result["retriever"]["documents"]

        return [{"content": d.content, "meta": d.meta} for d in docs]

    def build_context_bundle(
        self,
        task_description: str,
        top_k: int = 10,
    ) -> str:
        """Retrieve docs and format them into a single Markdown string."""
        docs = self.get_relevant_docs(task_description, top_k)
        if not docs:
            return (
                "No relevant context found in repository or "
                "approved vault notes."
            )

        lines = ["# Relevant Context", ""]
        for i, doc in enumerate(docs, 1):
            source = doc["meta"].get("source", "unknown")
            path = doc["meta"].get("path", "unknown")
            lines.append(f"## [{source}] {path}")
            lines.append("```")
            lines.append(doc["content"])
            lines.append("```")
            lines.append("")

        return "\n".join(lines)
