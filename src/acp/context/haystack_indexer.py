"""Haystack indexer (M6).

Uses Haystack V2 to build an ephemeral in-memory vector store of the repository
and the Obsidian vault. It strictly enforces the human firewall by ignoring
any task reports in the vault that do not have ``approved: true``.

Embeddings are generated locally via sentence-transformers to maintain the
Mac-first, air-gapped security posture.

This module requires the ``rag`` optional dependency group::

    uv sync --extra rag

If the dependencies are not installed, importing this module raises
``ImportError`` — the caller (``build_context_node``) catches that and falls
back to the M1 prompt-only behavior.
"""

from __future__ import annotations

from pathlib import Path

from haystack import Document, Pipeline
from haystack.components.embedders import SentenceTransformersDocumentEmbedder
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.document_stores.in_memory import InMemoryDocumentStore

from acp.config import ContextSection
from acp.context.scanner import scan_context
from acp.vault.frontmatter import parse_frontmatter


class HaystackIndexer:
    """Index repository and vault content for Haystack retrieval.

    The index is ephemeral — it lives only for the duration of a single task
    run. This keeps the system stateless and avoids stale-index bugs.
    """

    def __init__(
        self,
        repo_path: Path,
        vault_root: Path,
        context_config: ContextSection,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.vault_root = Path(vault_root)
        self.context_config = context_config
        self.document_store = InMemoryDocumentStore()

    def build_index(self) -> None:
        """Scan, split, embed, and store documents in memory."""
        raw_docs: list[Document] = []

        # 1. Gather Repository Code
        for path in scan_context(self.repo_path, self.context_config):
            try:
                content = path.read_text(encoding="utf-8")
                rel_path = path.relative_to(self.repo_path)
                raw_docs.append(
                    Document(
                        content=content,
                        meta={"source": "repo", "path": str(rel_path)},
                    )
                )
            except (UnicodeDecodeError, OSError):
                # Skip binary files or unreadable paths
                continue

        # 2. Gather Vault Notes (enforcing the human firewall)
        if self.vault_root.exists():
            for md_file in self.vault_root.rglob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")

                    # If it's a task report, it MUST be approved to enter context
                    if "tasks" in md_file.parts:
                        try:
                            fm, _ = parse_frontmatter(content)
                            if not fm.approved:
                                continue  # Skip unapproved agent claims
                        except ValueError:
                            continue  # Skip malformed frontmatter

                    rel_path = md_file.relative_to(self.vault_root)
                    raw_docs.append(
                        Document(
                            content=content,
                            meta={"source": "vault", "path": str(rel_path)},
                        )
                    )
                except (UnicodeDecodeError, OSError):
                    continue

        if not raw_docs:
            return

        # 3. Build and run the indexing pipeline
        indexing_pipeline = Pipeline()

        # Splitter: breaks large files into 250-word chunks with 25-word overlap
        indexing_pipeline.add_component(
            "splitter",
            DocumentSplitter(
                split_by="word", split_length=250, split_overlap=25,
            ),
        )
        # Embedder: uses the lightweight local MiniLM model
        indexing_pipeline.add_component(
            "embedder",
            SentenceTransformersDocumentEmbedder(model="all-MiniLM-L6-v2"),
        )
        # Writer: saves to our InMemoryDocumentStore
        indexing_pipeline.add_component(
            "writer",
            DocumentWriter(document_store=self.document_store),
        )

        # Wire the pipeline together
        indexing_pipeline.connect("splitter", "embedder")
        indexing_pipeline.connect("embedder", "writer")

        # Execute
        indexing_pipeline.run({"splitter": {"documents": raw_docs}})
