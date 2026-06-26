"""Context builder (M6, extended v0.7.0).

Uses Haystack V2 to retrieve relevant context for a coding task. Connects
a text embedder to an in-memory retriever, pulling from the index generated
by the :class:`HaystackIndexer`.

The output is a formatted Markdown string (``context_bundle.md``) that is
written to the artifacts directory and prepended to the agent's prompt.
Because it lands in ``artifacts/``, it is automatically hash-chained and
cryptographically signed by the evidence engine.

v0.7.0 (Phase 4.2): Optional cross-encoder re-ranking. When
``RerankingSection.enabled`` is True, a cross-encoder model re-scores
the initially retrieved chunks against the specific task description,
improving signal-to-noise. Both the original retrieval score and the
re-rank score are recorded in ``context_bundle.md`` so the evidence
trail shows which chunks were promoted or demoted.

This module requires the ``rag`` optional dependency group::

    uv sync --extra rag
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from haystack import Pipeline
from haystack.components.embedders import SentenceTransformersTextEmbedder
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever

from acp.config import ContextSection, RerankingSection
from acp.context.haystack_indexer import HaystackIndexer

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Build context bundles for coding tasks using Haystack retrieval.

    Args:
        repo_path: Path to the repository to index.
        vault_root: Path to the Obsidian vault root.
        context_config: Context section config.
        reranking_config: Optional re-ranking config (v0.7.0). When
            provided and ``enabled`` is True, a cross-encoder re-ranker
            is inserted after the initial vector search.
    """

    def __init__(
        self,
        repo_path: Path,
        vault_root: Path,
        context_config: ContextSection,
        reranking_config: RerankingSection | None = None,
    ) -> None:
        self.indexer = HaystackIndexer(
            repo_path, vault_root, context_config,
        )
        self.reranking_config = reranking_config

    def get_relevant_docs(
        self,
        task_description: str,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Run the query pipeline to fetch the most relevant text chunks.

        When re-ranking is enabled, this retrieves ``top_k_before_rerank``
        chunks and then re-ranks them with a cross-encoder, returning only
        the top ``top_k_after_rerank`` chunks. Each doc dict includes
        ``retrieval_score`` and (when re-ranked) ``rerank_score`` fields.
        """
        # 1. Build the index if it hasn't been built yet
        if self.indexer.document_store.count_documents() == 0:
            self.indexer.build_index()

        # If the repository/vault is completely empty, short-circuit
        if self.indexer.document_store.count_documents() == 0:
            return []

        # Determine how many chunks to retrieve before re-ranking.
        retrieval_k = top_k
        if (
            self.reranking_config
            and self.reranking_config.enabled
        ):
            retrieval_k = self.reranking_config.top_k_before_rerank

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
                document_store=self.indexer.document_store, top_k=retrieval_k,
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

        # Convert to dicts with retrieval scores.
        doc_dicts = []
        for d in docs:
            doc_dicts.append({
                "content": d.content,
                "meta": d.meta,
                "retrieval_score": float(getattr(d, "score", 0.0)),
            })

        # 4. v0.7.0: Optional cross-encoder re-ranking.
        if (
            self.reranking_config
            and self.reranking_config.enabled
            and len(doc_dicts) > 0
        ):
            doc_dicts = self._rerank(
                doc_dicts, task_description,
                self.reranking_config.top_k_after_rerank,
                self.reranking_config.model,
            )

        return doc_dicts

    def _rerank(
        self,
        docs: list[dict[str, Any]],
        query: str,
        top_k: int,
        model_name: str,
    ) -> list[dict[str, Any]]:
        """Re-rank docs using a cross-encoder model.

        Uses :mod:`sentence_transformers` ``CrossEncoder`` to re-score
        each (query, document) pair. The cross-encoder is more accurate
        than bi-encoder retrieval because it processes the query and
        document together, capturing fine-grained semantic interactions.

        Both the original ``retrieval_score`` and the new ``rerank_score``
        are preserved in each doc dict, so the evidence trail in
        ``context_bundle.md`` shows which chunks were promoted or demoted.
        """
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            # If sentence-transformers isn't available, return the
            # original docs without re-ranking (graceful degradation).
            logger.warning(
                "Re-ranking skipped: sentence-transformers not installed. "
                "Install with: uv sync --extra rag"
            )
            return docs[:top_k]

        try:
            model = CrossEncoder(model_name)
            pairs = [(query, d["content"]) for d in docs]
            scores = model.predict(pairs)
        except Exception as exc:  # noqa: BLE001
            # Model loading or prediction failed — degrade gracefully.
            logger.warning(
                "Re-ranking failed (model=%s): %s. Using original retrieval order.",
                model_name, exc,
            )
            return docs[:top_k]

        # Attach rerank scores and sort by them (descending).
        for i, doc in enumerate(docs):
            doc["rerank_score"] = float(scores[i])

        reranked = sorted(docs, key=lambda d: d["rerank_score"], reverse=True)
        return reranked[:top_k]

    def build_context_bundle(
        self,
        task_description: str,
        top_k: int = 10,
    ) -> str:
        """Retrieve docs and format them into a single Markdown string.

        When re-ranking is enabled, each chunk's header includes both
        the retrieval score and the re-rank score, making the evidence
        trail transparent about which chunks were promoted or demoted
        by the cross-encoder.
        """
        docs = self.get_relevant_docs(task_description, top_k)
        if not docs:
            return (
                "No relevant context found in repository or "
                "approved vault notes."
            )

        has_rerank = any("rerank_score" in d for d in docs)

        lines = ["# Relevant Context", ""]
        if has_rerank:
            lines.append(
                "Chunks re-ranked with cross-encoder. "
                "Both retrieval and re-rank scores shown."
            )
            lines.append("")

        for i, doc in enumerate(docs, 1):
            source = doc["meta"].get("source", "unknown")
            path = doc["meta"].get("path", "unknown")
            retrieval_score = doc.get("retrieval_score", 0.0)
            if has_rerank and "rerank_score" in doc:
                rerank_score = doc["rerank_score"]
                lines.append(
                    f"## [{source}] {path} "
                    f"(retrieval: {retrieval_score:.4f}, "
                    f"rerank: {rerank_score:.4f})"
                )
            else:
                lines.append(
                    f"## [{source}] {path} "
                    f"(score: {retrieval_score:.4f})"
                )
            lines.append("```")
            lines.append(doc["content"])
            lines.append("```")
            lines.append("")

        return "\n".join(lines)
