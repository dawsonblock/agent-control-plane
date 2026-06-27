"""v0.7.2 Phase 3 — Persistent & Incremental RAG tests.

Tests the persistent document store and incremental indexing feature:

  1. repo_index_path — deterministic, per-repo index directory computation
  2. HaystackIndexer persistent mode — saves/loads the document store + digest
     cache to disk, enabling incremental indexing (unchanged files skip
     re-embedding, deleted files are pruned).
  3. ContextBuilder — exposes index_stats and forwards persist_path to the
     indexer.
  4. DigestCache integration — the persisted digest cache contains entries for
     indexed files.

The heavy sentence-transformers embedding model is mocked with a lightweight
fake component so tests stay fast. The persistence, incremental, and digest
logic under test is entirely real.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from acp.config import ContextSection

# --------------------------------------------------------------------------- #
# Helper: check if haystack is installed
# --------------------------------------------------------------------------- #

try:
    import haystack  # noqa: F401

    HAYSTACK_INSTALLED = True
except ImportError:
    HAYSTACK_INSTALLED = False

rag_skip = pytest.mark.skipif(
    not HAYSTACK_INSTALLED,
    reason="rag extra not installed (uv sync --extra rag)",
)


# --------------------------------------------------------------------------- #
# Fake embedder — replaces the heavy sentence-transformers model
# --------------------------------------------------------------------------- #
#
# The real SentenceTransformersDocumentEmbedder downloads/loads a ~90 MB model.
# We swap it out with a deterministic fake that assigns a fixed-length zero
# vector to every document. This keeps the persistence & incremental logic
# under test real while making the suite fast enough for CI.


# The fake embedder must be defined at module level so that Haystack's
# component machinery (which calls ``typing.get_type_hints`` against the
# ``run`` method's ``__globals__``) can resolve the ``Document`` name.
if HAYSTACK_INSTALLED:
    from haystack import Document, component

    @component
    class _FakeEmbedder:
        """Lightweight stand-in for SentenceTransformersDocumentEmbedder.

        Assigns a fixed-length zero vector to every document so the indexing
        pipeline can run without loading a ~90 MB model.
        """

        def __init__(self, *args, **kwargs) -> None:
            pass

        @component.output_types(documents=list[Document])
        def run(self, documents: list[Document]) -> dict[str, list[Document]]:
            for doc in documents:
                doc.embedding = [0.0] * 16
            return {"documents": documents}


def _install_fake_embedder():
    """Return a patcher that replaces SentenceTransformersDocumentEmbedder."""
    return patch(
        "acp.context.haystack_indexer.SentenceTransformersDocumentEmbedder",
        _FakeEmbedder,
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def small_repo(tmp_path: Path) -> Path:
    """A tiny repo with two .py files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auth.py").write_text("def login(user, password):\n    return True\n")
    (repo / "utils.py").write_text("def helper():\n    return 42\n")
    return repo


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    """An empty vault directory."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


@pytest.fixture
def default_cfg() -> ContextSection:
    return ContextSection(include=["*.py"], exclude=[])


# --------------------------------------------------------------------------- #
# 1. repo_index_path tests
# --------------------------------------------------------------------------- #


class TestRepoIndexPath:
    """repo_index_path computes a deterministic, per-repo index directory."""

    def test_repo_index_path_default_base(self, tmp_path: Path) -> None:
        """Default base_dir is data/context_index/."""
        from acp.context.haystack_indexer import repo_index_path

        result = repo_index_path(tmp_path / "myrepo")

        parts = Path(result).parts
        assert "data" in parts
        assert "context_index" in parts

    def test_repo_index_path_custom_base(self, tmp_path: Path) -> None:
        """A custom base_dir is honored."""
        from acp.context.haystack_indexer import repo_index_path

        custom = tmp_path / "custom"
        result = repo_index_path(tmp_path / "myrepo", base_dir=custom)

        assert Path(result).is_relative_to(custom)

    def test_repo_index_path_different_repos(self, tmp_path: Path) -> None:
        """Different repo paths produce different index paths."""
        from acp.context.haystack_indexer import repo_index_path

        base = tmp_path / "idx"
        a = repo_index_path(tmp_path / "repo_a", base_dir=base)
        b = repo_index_path(tmp_path / "repo_b", base_dir=base)
        assert a != b

    def test_repo_index_path_same_repo(self, tmp_path: Path) -> None:
        """Same repo path produces the same index path (deterministic)."""
        from acp.context.haystack_indexer import repo_index_path

        base = tmp_path / "idx"
        repo = tmp_path / "repo"
        a = repo_index_path(repo, base_dir=base)
        b = repo_index_path(repo, base_dir=base)
        assert a == b


# --------------------------------------------------------------------------- #
# 2. HaystackIndexer persistent mode tests
# --------------------------------------------------------------------------- #


@rag_skip
class TestHaystackIndexerPersistent:
    """Persistent document store + incremental indexing."""

    def test_indexer_no_persist_path(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection
    ) -> None:
        """Without persist_path, every scanned file is a new file."""
        from acp.context.haystack_indexer import HaystackIndexer

        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
            )
            indexer.build_index()

        stats = indexer.index_stats
        assert stats["total_files"] == 2
        assert stats["new_files"] == 2
        assert stats["cached_files"] == 0
        assert stats["deleted_files"] == 0

    def test_indexer_persist_creates_dir(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """build_index creates the persist_path directory."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        assert not persist.exists()

        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

        assert persist.is_dir()

    def test_indexer_persist_saves_files(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """After build_index, document_store.json and digest_cache.json exist."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

        assert (persist / "document_store.json").is_file()
        assert (persist / "digest_cache.json").is_file()

    def test_indexer_persist_loads_existing(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """A new indexer with the same persist_path loads the saved documents."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            first = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            first.build_index()
            count_after_build = first.document_store.count_documents()
            assert count_after_build > 0

        # Create a second indexer pointing at the same persist_path.
        with _install_fake_embedder():
            second = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            # Documents should already be loaded (before any build_index call).
            assert second.document_store.count_documents() == count_after_build

    def test_indexer_incremental_cached_files(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """Second build with unchanged files reports cached_files > 0, new_files == 0."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()
            first_stats = indexer.index_stats
            assert first_stats["new_files"] == 2

            # Second build — nothing changed.
            indexer.build_index()
            second_stats = indexer.index_stats

        assert second_stats["cached_files"] == 2
        assert second_stats["new_files"] == 0
        assert second_stats["deleted_files"] == 0

    def test_indexer_incremental_new_file(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """Adding a file and rebuilding reports new_files > 0 for the new file."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

            # Add a new file.
            (small_repo / "new.py").write_text("def new_func():\n    return 'new'\n")
            indexer.build_index()
            stats = indexer.index_stats

        assert stats["new_files"] == 1
        assert stats["cached_files"] == 2
        assert stats["deleted_files"] == 0
        assert stats["total_files"] == 3

    def test_indexer_incremental_deleted_file(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """Deleting a file and rebuilding reports deleted_files > 0."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

            # Delete a file.
            (small_repo / "utils.py").unlink()
            indexer.build_index()
            stats = indexer.index_stats

        assert stats["deleted_files"] == 1
        assert stats["cached_files"] == 1
        assert stats["new_files"] == 0
        assert stats["total_files"] == 1

    def test_indexer_stats_total_files(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """index_stats['total_files'] matches the number of scanned files."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

        # small_repo has exactly 2 .py files.
        assert indexer.index_stats["total_files"] == 2

    def test_indexer_persist_includes_vault_notes(
        self, small_repo: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """Vault .md notes are indexed and persisted alongside repo files."""
        from acp.context.haystack_indexer import HaystackIndexer

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "rules").mkdir()
        (vault / "rules" / "style.md").write_text("# Style\nUse type hints.\n")

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

        # 2 repo files + 1 vault note.
        assert indexer.index_stats["total_files"] == 3
        assert (persist / "document_store.json").is_file()


# --------------------------------------------------------------------------- #
# 3. ContextBuilder tests
# --------------------------------------------------------------------------- #


@rag_skip
class TestContextBuilderPersist:
    """ContextBuilder exposes index_stats and forwards persist_path."""

    def test_context_builder_index_stats(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """ContextBuilder exposes the index_stats property."""
        from acp.context.context_builder import ContextBuilder

        with _install_fake_embedder():
            builder = ContextBuilder(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=tmp_path / "index",
            )
            # Before building, stats are all zero.
            assert builder.index_stats == {
                "total_files": 0,
                "new_files": 0,
                "cached_files": 0,
                "deleted_files": 0,
            }
            builder.indexer.build_index()

        stats = builder.index_stats
        assert stats["total_files"] == 2
        assert stats["new_files"] == 2

    def test_context_builder_with_persist_path(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """ContextBuilder forwards persist_path to the HaystackIndexer."""
        from acp.context.context_builder import ContextBuilder

        persist = tmp_path / "ctx_index"
        with _install_fake_embedder():
            builder = ContextBuilder(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            builder.indexer.build_index()

        # The indexer should have persisted to the given path.
        assert builder.indexer.persist_path == persist
        assert (persist / "document_store.json").is_file()
        assert (persist / "digest_cache.json").is_file()


# --------------------------------------------------------------------------- #
# 4. DigestCache integration
# --------------------------------------------------------------------------- #


@rag_skip
class TestDigestCacheIntegration:
    """The persisted digest cache contains entries for indexed files."""

    def test_indexer_uses_digest_cache(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """After indexing, digest_cache.json contains entries for indexed files."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            indexer = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            indexer.build_index()

        cache_path = persist / "digest_cache.json"
        assert cache_path.is_file()

        data = json.loads(cache_path.read_text())
        # The cache is keyed by absolute file paths.
        keys = list(data.keys())
        assert len(keys) >= 2
        # Each entry has the expected DigestRecord fields.
        for key, rec in data.items():
            assert "size" in rec
            assert "mtime_ns" in rec
            assert "sha256" in rec
            assert isinstance(rec["sha256"], str)
            assert len(rec["sha256"]) == 64  # SHA-256 hex

        # The indexed .py files should appear in the cache.
        auth_py = str(small_repo / "auth.py")
        utils_py = str(small_repo / "utils.py")
        assert auth_py in data
        assert utils_py in data

    def test_indexer_digest_cache_reused_across_runs(
        self, small_repo: Path, empty_vault: Path, default_cfg: ContextSection, tmp_path: Path
    ) -> None:
        """The digest cache is loaded and reused on the second run."""
        from acp.context.haystack_indexer import HaystackIndexer

        persist = tmp_path / "index"
        with _install_fake_embedder():
            first = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            first.build_index()

            # The in-memory cache should have records.
            assert len(first._digest_cache._records) >= 2

            # A fresh indexer loads the cache from disk.
            second = HaystackIndexer(
                repo_path=small_repo,
                vault_root=empty_vault,
                context_config=default_cfg,
                persist_path=persist,
            )
            assert len(second._digest_cache._records) >= 2
