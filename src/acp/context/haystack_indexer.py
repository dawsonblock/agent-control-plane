"""Haystack indexer (M6, extended v0.7.2 — Persistent & Incremental RAG).

Uses Haystack V2 to build a vector store of the repository and the Obsidian
vault. It strictly enforces the human firewall by ignoring any task reports
in the vault that do not have ``approved: true``.

Embeddings are generated locally via sentence-transformers to maintain the
Mac-first, air-gapped security posture.

v0.7.2 (Phase 3 — Persistent & Incremental RAG): The indexer now supports
a **persistent** document store backed by a JSON file on disk
(``data/context_index/``). This enables incremental indexing — unchanged
files (detected via DigestCache) skip re-embedding, making ``acp run``
near-instantaneous for repos where most files haven't changed.

The persistent store is a simple JSON-backed ``InMemoryDocumentStore``
that serializes to disk. This avoids adding chromadb as a dependency
while still providing persistence. The store is per-repo (keyed by
repo path hash) so multiple repos don't collide.

This module requires the ``rag`` optional dependency group::

    uv sync --extra rag

If the dependencies are not installed, importing this module raises
``ImportError`` — the caller (``build_context_node``) catches that and falls
back to the M1 prompt-only behavior.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from haystack import Document, Pipeline
from haystack.components.embedders import SentenceTransformersDocumentEmbedder
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.document_stores.in_memory import InMemoryDocumentStore

from acp.config import ContextSection
from acp.context.scanner import scan_context
from acp.evidence.manifest import DigestCache
from acp.vault.frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)


class HaystackIndexer:
    """Index repository and vault content for Haystack retrieval.

    v0.7.2: Supports persistent, incremental indexing. When
    ``persist_path`` is provided, the document store is saved to disk
    and reused across runs. Unchanged files (detected via DigestCache)
    skip re-embedding, making subsequent runs near-instantaneous.

    Args:
        repo_path: Path to the repository to index.
        vault_root: Path to the Obsidian vault root.
        context_config: Context section config.
        persist_path: Optional path to persist the index. When set,
            the index survives across runs and is incrementally updated.
    """

    def __init__(
        self,
        repo_path: Path,
        vault_root: Path,
        context_config: ContextSection,
        *,
        persist_path: Path | None = None,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.vault_root = Path(vault_root)
        self.context_config = context_config
        self.persist_path = persist_path
        self.document_store = InMemoryDocumentStore()
        self._digest_cache = DigestCache()
        self._index_stats: dict[str, int] = {
            "total_files": 0,
            "new_files": 0,
            "cached_files": 0,
            "deleted_files": 0,
        }

        # Load persistent state if available.
        if persist_path:
            self._load_persistent_state()

    @property
    def index_stats(self) -> dict[str, int]:
        """Statistics from the last ``build_index`` call.

        Includes:
          - ``total_files``: total files scanned
          - ``new_files``: files that were (re-)embedded
          - ``cached_files``: files skipped (unchanged since last index)
          - ``deleted_files``: files removed from the index (deleted from disk)
        """
        return dict(self._index_stats)

    def _load_persistent_state(self) -> None:
        """Load the persistent document store and digest cache from disk."""
        if not self.persist_path:
            return

        store_path = self.persist_path / "document_store.json"
        cache_path = self.persist_path / "digest_cache.json"

        # Load document store first. If it fails, we start fresh AND reset
        # the digest cache to avoid inconsistency (cache thinks files are
        # indexed but the store is empty).
        store_loaded = False
        if store_path.is_file():
            try:
                data = json.loads(store_path.read_text())
                docs = [Document.from_dict(d) for d in data.get("documents", [])]
                if docs:
                    self.document_store.write_documents(docs)
                store_loaded = True
                logger.debug(
                    "persistent RAG: loaded %d documents from %s",
                    len(docs),
                    store_path,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("persistent RAG: failed to load store: %s — starting fresh", exc)

        # Only load the digest cache if the document store loaded successfully.
        # If the store failed, a stale cache would cause incorrect "cached"
        # stats — the cache says files are indexed but they're not.
        if store_loaded:
            self._digest_cache = DigestCache.load_from(cache_path)
        else:
            self._digest_cache = DigestCache()

    def _save_persistent_state(self) -> None:
        """Save the document store and digest cache to disk."""
        if not self.persist_path:
            return

        self.persist_path.mkdir(parents=True, exist_ok=True)
        store_path = self.persist_path / "document_store.json"
        cache_path = self.persist_path / "digest_cache.json"

        # Save document store.
        try:
            docs = self.document_store.filter_documents()
            data = {"documents": [d.to_dict() for d in docs]}
            tmp = store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, default=str))
            tmp.rename(store_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persistent RAG: failed to save store: %s", exc)

        # Save digest cache.
        try:
            self._digest_cache.save_to(cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persistent RAG: failed to save digest cache: %s", exc)

    def _file_digest(self, path: Path) -> str:
        """Get the SHA-256 digest of a file, using the cache if possible."""
        return self._digest_cache.digest(path)

    def _get_indexed_file_hashes(self) -> dict[str, str]:
        """Return a mapping of file paths to their indexed content hashes.

        Extracted from the document store's metadata — each document's
        ``meta["file_hash"]`` records the hash of the source file at
        indexing time.
        """
        result: dict[str, str] = {}
        try:
            docs = self.document_store.filter_documents()
            for doc in docs:
                file_hash = doc.meta.get("file_hash")
                file_path = doc.meta.get("path")
                if file_hash and file_path:
                    result[file_path] = file_hash
        except Exception as exc:  # noqa: BLE001
            logger.warning("persistent RAG: failed to get indexed file hashes: %s", exc)
        return result

    def _delete_documents_for_file(self, file_path: str) -> None:
        """Delete all documents originating from a specific file."""
        try:
            docs = self.document_store.filter_documents()
            to_delete = [d.id for d in docs if d.meta.get("path") == file_path]
            if to_delete:
                self.document_store.delete_documents(to_delete)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persistent RAG: failed to delete docs for %s: %s", file_path, exc)

    def build_index(self) -> None:
        """Scan, split, embed, and store documents.

        v0.7.2: When persistent mode is active, only changed files are
        re-embedded. Unchanged files (same size + mtime) are skipped.
        Deleted files are removed from the index.
        """
        is_persistent = self.persist_path is not None
        self._index_stats = {
            "total_files": 0,
            "new_files": 0,
            "cached_files": 0,
            "deleted_files": 0,
        }

        # For persistent mode, fetch the indexed file hashes ONCE (not per
        # file) to avoid O(n²) complexity. This dict maps rel_path →
        # file_hash for all documents currently in the store.
        indexed_hashes: dict[str, str] = self._get_indexed_file_hashes() if is_persistent else {}

        # Gather all current files and their hashes.
        current_files: dict[str, str] = {}  # rel_path -> file_hash
        raw_docs: list[Document] = []

        # 1. Gather Repository Code
        for path in scan_context(self.repo_path, self.context_config):
            try:
                content = path.read_text(encoding="utf-8")
                rel_path = str(path.relative_to(self.repo_path))
                file_hash = self._file_digest(path)
                current_files[rel_path] = file_hash
                self._index_stats["total_files"] += 1

                if is_persistent:
                    if rel_path in indexed_hashes and indexed_hashes[rel_path] == file_hash:
                        self._index_stats["cached_files"] += 1
                        continue  # Skip — unchanged file
                    # File changed or new — delete old docs for this file.
                    self._delete_documents_for_file(rel_path)

                raw_docs.append(
                    Document(
                        content=content,
                        meta={
                            "source": "repo",
                            "path": rel_path,
                            "file_hash": file_hash,
                        },
                    )
                )
                self._index_stats["new_files"] += 1
            except (UnicodeDecodeError, OSError, ValueError):
                # ValueError: path not relative to repo_path (e.g., symlink)
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

                    rel_path = str(md_file.relative_to(self.vault_root))
                    file_hash = self._file_digest(md_file)
                    current_files[rel_path] = file_hash
                    self._index_stats["total_files"] += 1

                    if is_persistent:
                        if rel_path in indexed_hashes and indexed_hashes[rel_path] == file_hash:
                            self._index_stats["cached_files"] += 1
                            continue
                        self._delete_documents_for_file(rel_path)

                    raw_docs.append(
                        Document(
                            content=content,
                            meta={
                                "source": "vault",
                                "path": rel_path,
                                "file_hash": file_hash,
                            },
                        )
                    )
                    self._index_stats["new_files"] += 1
                except (UnicodeDecodeError, OSError, ValueError):
                    # ValueError: md_file not relative to vault_root (e.g., symlink)
                    continue

        # 3. Persistent mode: remove deleted files from the index.
        if is_persistent:
            for old_path in indexed_hashes:
                if old_path not in current_files:
                    self._delete_documents_for_file(old_path)
                    self._index_stats["deleted_files"] += 1

        if not raw_docs:
            # Even with no new docs, save state (digest cache may have updated).
            if is_persistent:
                self._save_persistent_state()
            return

        # 4. Build and run the indexing pipeline (only for new/changed docs)
        indexing_pipeline = Pipeline()

        indexing_pipeline.add_component(
            "splitter",
            DocumentSplitter(
                split_by="word",
                split_length=250,
                split_overlap=25,
            ),
        )
        indexing_pipeline.add_component(
            "embedder",
            SentenceTransformersDocumentEmbedder(model="all-MiniLM-L6-v2"),
        )
        indexing_pipeline.add_component(
            "writer",
            DocumentWriter(document_store=self.document_store),
        )

        indexing_pipeline.connect("splitter", "embedder")
        indexing_pipeline.connect("embedder", "writer")

        indexing_pipeline.run({"splitter": {"documents": raw_docs}})

        # 5. Persist the updated state.
        if is_persistent:
            self._save_persistent_state()
            logger.info(
                "persistent RAG: indexed %d new, %d cached, %d deleted, %d total",
                self._index_stats["new_files"],
                self._index_stats["cached_files"],
                self._index_stats["deleted_files"],
                self._index_stats["total_files"],
            )


def repo_index_path(repo_path: Path, base_dir: Path | None = None) -> Path:
    """Compute the persistent index directory for a repo.

    The directory is keyed by a hash of the repo's absolute path so
    multiple repos don't collide. Default base is ``data/context_index/``.
    """
    base = base_dir or Path("data/context_index")
    repo_hash = hashlib.sha256(str(repo_path.resolve()).encode()).hexdigest()[:16]
    return base / repo_hash
