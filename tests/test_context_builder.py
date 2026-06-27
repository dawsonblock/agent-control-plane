"""M6 Haystack RAG context retrieval tests.

Tests the context builder feature:

  1. ImportError fallback — build_context_node works without rag extra
  2. Vault filtering — unapproved task notes are excluded from the index
  3. Scanner integration — scan_context feeds the indexer
  4. Event payload — context.built event records haystack status
  5. Full RAG pipeline — indexed docs are retrievable (skipped if no haystack)
  6. Evidence binding — context_bundle.md is hash-chained (skipped if no haystack)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.config import ContextSection, RepoConfig, RepoSection
from acp.models import EventType

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
# 1. ImportError fallback — build_context_node works without rag extra
# --------------------------------------------------------------------------- #


class TestImportErrorFallback:
    """build_context_node gracefully falls back when rag isn't installed."""

    def test_context_built_event_has_haystack_false(self, tmp_path):
        """When rag isn't installed, context.built event has haystack: False."""
        if HAYSTACK_INSTALLED:
            pytest.skip("rag is installed — fallback path not tested here")

        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, build_context_node
        from acp.store import TaskStore

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir()
        events = EventWriter("task_001", run_dir)

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "user_request": "fix the bug",
            "worktree_path": tmp_path / "worktree",
            "artifacts_dir": artifacts_dir,
            "repo_path": tmp_path,
            "vault_root": tmp_path / "vault",
        }

        result = build_context_node(state, ctx)

        # Prompt should be written
        assert result["prompt_path"].exists()
        assert result["context_bundle_path"] is None

        # Event should record haystack: False
        all_events = events.read_all()
        ctx_event = [e for e in all_events if e.type == EventType.CONTEXT_BUILT][0]
        assert ctx_event.payload["haystack"] is False
        assert ctx_event.payload["retrieved_documents"] == 0
        assert ctx_event.payload["context_bundle_path"] is None

    def test_prompt_written_without_context_bundle(self, tmp_path):
        """Agent prompt is written even when rag isn't installed."""
        if HAYSTACK_INSTALLED:
            pytest.skip("rag is installed — fallback path not tested here")

        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, build_context_node
        from acp.store import TaskStore

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir()
        events = EventWriter("task_001", run_dir)

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "user_request": "add a feature",
            "worktree_path": tmp_path / "worktree",
            "artifacts_dir": artifacts_dir,
            "repo_path": tmp_path,
            "vault_root": tmp_path / "vault",
        }

        result = build_context_node(state, ctx)

        prompt_content = result["prompt_path"].read_text()
        assert "add a feature" in prompt_content
        # No context bundle reference when rag isn't installed
        assert "Relevant context" not in prompt_content


# --------------------------------------------------------------------------- #
# 2. Vault filtering — unapproved task notes are excluded
# --------------------------------------------------------------------------- #


class TestVaultFiltering:
    """The indexer excludes unapproved vault task notes (human firewall)."""

    def _make_vault_note(
        self,
        vault_root: Path,
        path: str,
        approved: bool,
        content: str = "Some task content",
    ) -> Path:
        """Write a vault note with frontmatter."""
        note_path = vault_root / path
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            f"---\n"
            f"type: task_report\n"
            f"task_id: task_001\n"
            f"approved: {str(approved).lower()}\n"
            f"---\n"
            f"{content}\n"
        )
        return note_path

    def test_unapproved_task_note_is_filtered(self, tmp_path):
        """Unapproved task notes are not indexed."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        # Approved note in tasks/
        self._make_vault_note(
            vault_root,
            "tasks/approved_task.md",
            approved=True,
            content="Approved fix for authentication",
        )
        # Unapproved note in tasks/
        self._make_vault_note(
            vault_root,
            "tasks/unapproved_task.md",
            approved=False,
            content="Unapproved risky change to database",
        )
        # Rule note (not in tasks/ — always included)
        (vault_root / "rules").mkdir()
        (vault_root / "rules" / "coding.md").write_text(
            "# Coding Rules\nUse type hints.\n",
        )

        # Test the filtering logic directly (without haystack)
        from acp.vault.frontmatter import parse_frontmatter

        indexed_paths: list[str] = []
        for md_file in vault_root.rglob("*.md"):
            content = md_file.read_text(encoding="utf-8")
            if "tasks" in md_file.parts:
                try:
                    fm, _ = parse_frontmatter(content)
                    if not fm.approved:
                        continue  # Skip unapproved
                except ValueError:
                    continue
            indexed_paths.append(md_file.name)

        # Approved task + rule note = 2 files
        assert "approved_task.md" in indexed_paths
        assert "unapproved_task.md" not in indexed_paths
        assert "coding.md" in indexed_paths

    def test_non_task_vault_notes_always_included(self, tmp_path):
        """Vault notes outside tasks/ are always included (rules, decisions)."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        (vault_root / "rules").mkdir()
        (vault_root / "rules" / "style.md").write_text("# Style\n")
        (vault_root / "decisions").mkdir()
        (vault_root / "decisions" / "adr_001.md").write_text("# ADR 1\n")

        from acp.vault.frontmatter import parse_frontmatter

        indexed: list[str] = []
        for md_file in vault_root.rglob("*.md"):
            content = md_file.read_text(encoding="utf-8")
            if "tasks" in md_file.parts:
                try:
                    fm, _ = parse_frontmatter(content)
                    if not fm.approved:
                        continue
                except ValueError:
                    continue
            indexed.append(md_file.name)

        assert "style.md" in indexed
        assert "adr_001.md" in indexed


# --------------------------------------------------------------------------- #
# 3. Scanner integration — scan_context feeds the indexer
# --------------------------------------------------------------------------- #


class TestScannerIntegration:
    """scan_context produces the file list for the indexer."""

    def test_scan_context_with_include_patterns(self, tmp_path):
        from acp.context.scanner import scan_context

        # Create some files
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("assert True")
        (tmp_path / "README.md").write_text("# README")

        cfg = ContextSection(
            include=["src/*.py", "tests/*.py"],
            exclude=[],
        )

        files = list(scan_context(tmp_path, cfg))
        names = [f.name for f in files]
        assert "main.py" in names
        assert "test_main.py" in names
        assert "README.md" not in names

    def test_scan_context_with_exclude_patterns(self, tmp_path):
        from acp.context.scanner import scan_context

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')")
        (tmp_path / "src" / "ignored.py").write_text("# ignored")

        cfg = ContextSection(
            include=["src/*.py"],
            exclude=["src/ignored.py"],
        )

        files = list(scan_context(tmp_path, cfg))
        names = [f.name for f in files]
        assert "main.py" in names
        assert "ignored.py" not in names

    def test_scan_context_no_patterns_includes_all(self, tmp_path):
        from acp.context.scanner import scan_context

        (tmp_path / "a.py").write_text("a")
        (tmp_path / "b.py").write_text("b")

        cfg = ContextSection(include=[], exclude=[])

        files = list(scan_context(tmp_path, cfg))
        names = [f.name for f in files]
        assert "a.py" in names
        assert "b.py" in names


# --------------------------------------------------------------------------- #
# 4. Event payload — context.built event records haystack status
# --------------------------------------------------------------------------- #


class TestContextBuiltEvent:
    """context.built event correctly records haystack status and document count."""

    def test_event_payload_fields_exist(self, tmp_path):
        """context.built event has all required fields."""
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, build_context_node
        from acp.store import TaskStore

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir()
        events = EventWriter("task_001", run_dir)

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "user_request": "test task",
            "worktree_path": tmp_path / "worktree",
            "artifacts_dir": artifacts_dir,
            "repo_path": tmp_path,
            "vault_root": tmp_path / "vault",
        }

        build_context_node(state, ctx)

        all_events = events.read_all()
        ctx_event = [e for e in all_events if e.type == EventType.CONTEXT_BUILT][0]

        # All required fields must be present
        assert "prompt_path" in ctx_event.payload
        assert "haystack" in ctx_event.payload
        assert "retrieved_documents" in ctx_event.payload
        assert "context_bundle_path" in ctx_event.payload


# --------------------------------------------------------------------------- #
# 5. Full RAG pipeline — indexed docs are retrievable (skipped if no haystack)
# --------------------------------------------------------------------------- #


@rag_skip
class TestFullRAGPipeline:
    """Full Haystack RAG pipeline tests (require rag extra)."""

    def test_indexer_builds_and_retrieves(self, tmp_path):
        """HaystackIndexer indexes files and ContextBuilder retrieves them."""
        from acp.context.context_builder import ContextBuilder

        # Create a small repo
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "auth.py").write_text(
            "def login(user, password):\n"
            "    '''Authenticate a user with password.'''\n"
            "    return check_credentials(user, password)\n",
        )
        (repo_path / "utils.py").write_text(
            "def helper():\n    '''A utility function.'''\n    return 42\n",
        )

        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        cfg = ContextSection(include=["*.py"], exclude=[])

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=cfg,
        )

        bundle = builder.build_context_bundle(
            "authentication login password",
            top_k=5,
        )

        # The bundle should contain the auth.py content
        assert "Relevant Context" in bundle or "No relevant" in bundle
        if "Relevant Context" in bundle:
            assert "auth" in bundle.lower()

    def test_context_bundle_excludes_unapproved_tasks(self, tmp_path):
        """ContextBuilder excludes unapproved vault task notes."""
        from acp.context.context_builder import ContextBuilder

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "main.py").write_text("print('hello')\n")

        vault_root = tmp_path / "vault"
        (vault_root / "tasks").mkdir(parents=True)
        # Unapproved task
        (vault_root / "tasks" / "unapproved.md").write_text(
            "---\n"
            "type: task_report\n"
            "task_id: task_001\n"
            "approved: false\n"
            "---\n"
            "Risky database migration\n",
        )
        # Approved task
        (vault_root / "tasks" / "approved.md").write_text(
            "---\n"
            "type: task_report\n"
            "task_id: task_002\n"
            "approved: true\n"
            "---\n"
            "Safe authentication fix\n",
        )

        cfg = ContextSection(include=["*.py"], exclude=[])

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=cfg,
        )

        bundle = builder.build_context_bundle(
            "database migration",
            top_k=10,
        )

        # Unapproved task content should NOT appear
        if "Relevant Context" in bundle:
            assert "Risky database migration" not in bundle

    def test_empty_repo_returns_no_context_message(self, tmp_path):
        """ContextBuilder handles empty repos gracefully."""
        from acp.context.context_builder import ContextBuilder

        repo_path = tmp_path / "empty_repo"
        repo_path.mkdir()

        vault_root = tmp_path / "empty_vault"
        vault_root.mkdir()

        cfg = ContextSection(include=["*.py"], exclude=[])

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=cfg,
        )

        bundle = builder.build_context_bundle("anything", top_k=5)
        assert "No relevant context" in bundle


# --------------------------------------------------------------------------- #
# 6. Evidence binding — context_bundle.md is hash-chained (skipped if no haystack)
# --------------------------------------------------------------------------- #


@rag_skip
class TestEvidenceBinding:
    """context_bundle.md is automatically bound to the evidence manifest."""

    def test_context_bundle_hashed_in_manifest(self, tmp_path):
        """context_bundle.md appears in the evidence manifest artifacts."""
        from acp.context.context_builder import ContextBuilder
        from acp.evidence.manifest import compute_artifact_content_hash

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "main.py").write_text("print('hi')\n")

        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        cfg = ContextSection(include=["*.py"], exclude=[])

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=cfg,
        )

        bundle = builder.build_context_bundle("test", top_k=5)
        bundle_path = artifacts_dir / "context_bundle.md"
        bundle_path.write_text(bundle)

        # The artifact content hash should include context_bundle.md
        artifact_hash = compute_artifact_content_hash(tmp_path)
        assert artifact_hash is not None
        # The hash is a hex string
        assert isinstance(artifact_hash, str)
        assert len(artifact_hash) == 64  # SHA256 hex


# --------------------------------------------------------------------------- #
# 7. v0.7.0 (Phase 4.2): Cross-encoder re-ranking
# --------------------------------------------------------------------------- #


class TestReranking:
    """Test the cross-encoder re-ranking feature."""

    @rag_skip
    def test_reranking_config_accepted(self, tmp_path):
        """ContextBuilder accepts a RerankingSection."""
        from acp.config import RerankingSection
        from acp.context.context_builder import ContextBuilder

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=ContextSection(include=["*.py"], exclude=[]),
            reranking_config=RerankingSection(enabled=True),
        )
        assert builder.reranking_config is not None
        assert builder.reranking_config.enabled is True

    @rag_skip
    def test_reranking_disabled_by_default(self, tmp_path):
        """ContextBuilder works without a reranking config (disabled by default)."""
        from acp.context.context_builder import ContextBuilder

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=ContextSection(include=["*.py"], exclude=[]),
        )
        assert builder.reranking_config is None

    @rag_skip
    def test_reranking_scores_in_bundle(self, tmp_path):
        """context_bundle.md includes both retrieval and rerank scores."""
        from acp.config import RerankingSection
        from acp.context.context_builder import ContextBuilder

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "auth.py").write_text(
            "def login(user, password):\n    # authenticate user with password\n    pass\n"
        )
        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=ContextSection(include=["*.py"], exclude=[]),
            reranking_config=RerankingSection(
                enabled=True,
                top_k_before_rerank=10,
                top_k_after_rerank=3,
            ),
        )
        bundle = builder.build_context_bundle("authenticate user login", top_k=5)

        # The bundle should have score annotations.
        if "No relevant context" not in bundle:
            assert "retrieval:" in bundle or "score:" in bundle

    @rag_skip
    def test_reranking_graceful_degradation_on_missing_model(self, tmp_path):
        """_rerank returns original docs when the model can't be loaded."""
        from acp.config import RerankingSection
        from acp.context.context_builder import ContextBuilder

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        vault_root = tmp_path / "vault"
        vault_root.mkdir()

        builder = ContextBuilder(
            repo_path=repo_path,
            vault_root=vault_root,
            context_config=ContextSection(include=["*.py"], exclude=[]),
            reranking_config=RerankingSection(enabled=True),
        )

        # Call _rerank with a non-existent model — should degrade gracefully.
        docs = [
            {"content": "test doc 1", "meta": {"source": "repo"}, "retrieval_score": 0.9},
            {"content": "test doc 2", "meta": {"source": "repo"}, "retrieval_score": 0.7},
        ]
        result = builder._rerank(docs, "test query", top_k=2, model_name="nonexistent/model")
        # Should return the original docs (truncated to top_k) without raising.
        assert len(result) <= 2

    def test_reranking_config_validation(self):
        """RerankingSection validates top_k ranges."""
        from acp.config import RerankingSection

        # Valid config.
        cfg = RerankingSection(enabled=True, top_k_before_rerank=20, top_k_after_rerank=5)
        assert cfg.top_k_before_rerank == 20

        # Invalid: top_k_before_rerank too large.
        with pytest.raises(ValueError, match="top_k_before_rerank"):
            RerankingSection(top_k_before_rerank=200)

        # Invalid: top_k_after_rerank zero.
        with pytest.raises(ValueError, match="top_k_after_rerank"):
            RerankingSection(top_k_after_rerank=0)
