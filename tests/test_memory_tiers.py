"""Tests for cognitive memory tiers (v0.6.9).

Tests the three-tier memory model: Working, Episodic, and Semantic.
The episodic memory store tests use real event logs on disk. The
working and semantic tier tests verify graceful fallback when the
optional extras (rag, memory) aren't installed.
"""

from __future__ import annotations

import json
from pathlib import Path

from acp.memory.tiers import (
    CognitiveMemoryRetriever,
    EpisodicMemoryStore,
    MemoryBundle,
    MemoryItem,
)


# --------------------------------------------------------------------------- #
# MemoryItem + MemoryBundle tests
# --------------------------------------------------------------------------- #


class TestMemoryBundle:
    """Test the MemoryBundle data structure."""

    def test_empty_bundle_prompt_is_empty(self):
        bundle = MemoryBundle()
        assert bundle.to_prompt_section() == ""
        assert bundle.total_items == 0

    def test_bundle_with_items(self):
        bundle = MemoryBundle(
            working=[MemoryItem(tier="working", source="rag", content="auth.py uses OAuth2")],
            episodic=[MemoryItem(tier="episodic", source="events", content="Last auth task failed", metadata={"task_id": "task_001"})],
            semantic=[MemoryItem(tier="semantic", source="graphiti", content="Auth module uses PKCE flow")],
        )
        assert bundle.total_items == 3
        section = bundle.to_prompt_section()
        assert "Working Memory" in section
        assert "Episodic Memory" in section
        assert "Semantic Memory" in section
        assert "auth.py uses OAuth2" in section
        assert "task_001" in section
        assert "PKCE flow" in section

    def test_bundle_with_only_working(self):
        bundle = MemoryBundle(
            working=[MemoryItem(tier="working", source="rag", content="test")],
        )
        section = bundle.to_prompt_section()
        assert "Working Memory" in section
        assert "Episodic Memory" not in section


# --------------------------------------------------------------------------- #
# EpisodicMemoryStore tests
# --------------------------------------------------------------------------- #


class TestEpisodicMemoryStore:
    """Test the cross-run episodic memory store."""

    def _create_run(
        self,
        runs_root: Path,
        task_id: str,
        events: list[dict],
    ) -> None:
        """Create a run directory with an events.jsonl file."""
        run_dir = runs_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in events]
        (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")

    def test_recall_finds_matching_episodes(self, tmp_path: Path):
        """EpisodicMemoryStore finds past events matching the query."""
        runs_root = tmp_path / "runs"
        self._create_run(runs_root, "task_20260625_0001", [
            {"type": "task.created", "payload": {"user_request": "Add OAuth to auth module"}},
        ])
        self._create_run(runs_root, "task_20260625_0002", [
            {"type": "task.created", "payload": {"user_request": "Fix database migration"}},
        ])

        store = EpisodicMemoryStore(runs_root)
        results = store.recall("OAuth")

        assert len(results) == 1
        assert results[0].tier == "episodic"
        assert "OAuth" in results[0].content
        assert results[0].metadata["task_id"] == "task_20260625_0001"

    def test_recall_returns_empty_when_no_matches(self, tmp_path: Path):
        """EpisodicMemoryStore returns empty list when nothing matches."""
        runs_root = tmp_path / "runs"
        self._create_run(runs_root, "task_20260625_0001", [
            {"type": "task.created", "payload": {"user_request": "Fix database migration"}},
        ])

        store = EpisodicMemoryStore(runs_root)
        results = store.recall("nonexistent_query_xyz")
        assert results == []

    def test_recall_returns_empty_when_no_runs(self, tmp_path: Path):
        """EpisodicMemoryStore returns empty when runs dir doesn't exist."""
        store = EpisodicMemoryStore(tmp_path / "nonexistent")
        assert store.recall("anything") == []

    def test_recall_respects_max_episodes(self, tmp_path: Path):
        """EpisodicMemoryStore limits results to max_episodes."""
        runs_root = tmp_path / "runs"
        for i in range(5):
            self._create_run(runs_root, f"task_20260625_{i:04d}", [
                {"type": "task.created", "payload": {"user_request": "auth change"}},
            ])

        store = EpisodicMemoryStore(runs_root)
        results = store.recall("auth", max_episodes=3)
        assert len(results) == 3

    def test_recall_most_recent_first(self, tmp_path: Path):
        """EpisodicMemoryStore returns most recent runs first."""
        runs_root = tmp_path / "runs"
        self._create_run(runs_root, "task_20260625_0001", [
            {"type": "task.created", "payload": {"user_request": "auth old"}},
        ])
        self._create_run(runs_root, "task_20260625_0002", [
            {"type": "task.created", "payload": {"user_request": "auth new"}},
        ])

        store = EpisodicMemoryStore(runs_root)
        results = store.recall("auth")
        assert len(results) == 2
        # Newest first (sorted reverse by dir name).
        assert results[0].metadata["task_id"] == "task_20260625_0002"
        assert results[1].metadata["task_id"] == "task_20260625_0001"

    def test_recall_skips_malformed_logs(self, tmp_path: Path):
        """EpisodicMemoryStore skips runs with malformed event logs."""
        runs_root = tmp_path / "runs"
        run_dir = runs_root / "task_20260625_0001"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text("not valid json\n")

        store = EpisodicMemoryStore(runs_root)
        results = store.recall("anything")
        assert results == []

    def test_summarize_event_types(self):
        """_summarize_event produces readable summaries for key event types."""
        from acp.memory.tiers import EpisodicMemoryStore as EMS
        assert "Task started" in EMS._summarize_event(
            "task.created", {"user_request": "Add tests"}, "task_001",
        )
        assert "Task failed" in EMS._summarize_event(
            "task.failed", {"error": "timeout"}, "task_001",
        )
        assert "Review" in EMS._summarize_event(
            "review.completed", {"risk": "high", "recommendation": "reject"}, "task_001",
        )
        assert "Auto-merge refused" in EMS._summarize_event(
            "auto.merge.refused", {"reason": "risk_exceeds_max"}, "task_001",
        )


# --------------------------------------------------------------------------- #
# CognitiveMemoryRetriever tests
# --------------------------------------------------------------------------- #


class TestCognitiveMemoryRetriever:
    """Test the unified three-tier retriever."""

    def test_retrieve_returns_bundle(self, tmp_path: Path):
        """CognitiveMemoryRetriever returns a MemoryBundle from all tiers."""
        runs_root = tmp_path / "runs"
        run_dir = runs_root / "task_20260625_0001"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text(
            json.dumps({"type": "task.created", "payload": {"user_request": "auth fix"}}) + "\n"
        )

        retriever = CognitiveMemoryRetriever(runs_root=runs_root)
        bundle = retriever.retrieve("auth")

        # Episodic should have results; working/semantic may be empty
        # (optional extras not installed).
        assert len(bundle.episodic) >= 1
        assert bundle.total_items >= 1

    def test_retrieve_graceful_when_empty(self, tmp_path: Path):
        """CognitiveMemoryRetriever returns empty bundle when no data."""
        retriever = CognitiveMemoryRetriever(runs_root=tmp_path / "nonexistent")
        bundle = retriever.retrieve("anything")
        assert bundle.total_items == 0
        assert bundle.to_prompt_section() == ""

    def test_retrieve_episodic_finds_past_failures(self, tmp_path: Path):
        """Episodic tier recalls past task failures for the query."""
        runs_root = tmp_path / "runs"
        run_dir = runs_root / "task_20260625_0001"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text(
            json.dumps({"type": "task.failed", "payload": {"error": "auth module import error"}}) + "\n"
        )

        retriever = CognitiveMemoryRetriever(runs_root=runs_root)
        bundle = retriever.retrieve("auth")
        assert len(bundle.episodic) >= 1
        assert any("auth" in item.content.lower() for item in bundle.episodic)
