"""M7 Graphiti temporal memory tests.

Tests the temporal memory feature:

  1. Promotion rules — should_promote, priority, exclusions, metadata
  2. Human firewall — rejects unapproved/archived/already-ingested notes
  3. CLI acp memory promote — dry run mode
  4. Auto-promotion — auto_approve_node with promote_reports_by_default
  5. Full Graphiti integration — ingest + search (skipped if no memory extra)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from acp.models import (
    EventType,
    MemoryStatus,
    Task,
    TaskStatus,
)
from acp.memory.promotion_rules import (
    PROMOTION_BLOCKED,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    PRIORITY_URGENT,
    get_promotion_exclusions,
    get_promotion_metadata,
    get_promotion_priority,
    should_promote_to_graphiti,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

try:
    import graphiti_core  # noqa: F401
    GRAPHITI_INSTALLED = True
except ImportError:
    GRAPHITI_INSTALLED = False

memory_skip = pytest.mark.skipif(
    not GRAPHITI_INSTALLED,
    reason="memory extra not installed (uv sync --extra memory)",
)


def _make_task(
    task_id: str = "task_20260626_0001",
    status: TaskStatus = TaskStatus.PASSED,
    repo_name: str = "test-repo",
) -> Task:
    return Task(
        task_id=task_id,
        repo_name=repo_name,
        repo_path=Path("/tmp/repo"),
        base_branch="main",
        task_branch=f"agent/{task_id}",
        worktree_path=Path("/tmp/worktree"),
        user_request="Fix the authentication bug",
        status=status,
    )


def _make_frontmatter(
    approved: bool = True,
    memory_status: str = MemoryStatus.ACTIVE.value,
    graphiti_ingested: bool = False,
    risk: str = "low",
    task_id: str = "task_20260626_0001",
) -> dict:
    return {
        "type": "task_report",
        "task_id": task_id,
        "approved": approved,
        "memory_status": memory_status,
        "graphiti_ingested": graphiti_ingested,
        "risk": risk,
        "recommendation": "merge",
        "status": "passed",
    }


def _write_vault_note(
    vault_root: Path,
    task_id: str,
    approved: bool = True,
    memory_status: str = "active",
    graphiti_ingested: bool = False,
    risk: str = "low",
    body: str = "## Report\nFixed the auth bug.",
) -> Path:
    """Write a vault note with the given frontmatter."""
    note_path = vault_root / "tasks" / f"{task_id}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        f"---\n"
        f"type: task_report\n"
        f"task_id: {task_id}\n"
        f"approved: {str(approved).lower()}\n"
        f"memory_status: {memory_status}\n"
        f"graphiti_ingested: {str(graphiti_ingested).lower()}\n"
        f"risk: {risk}\n"
        f"recommendation: merge\n"
        f"status: passed\n"
        f"---\n\n{body}\n"
    )
    return note_path


# --------------------------------------------------------------------------- #
# 1. Promotion rules
# --------------------------------------------------------------------------- #


class TestShouldPromoteToGraphiti:
    """should_promote_to_graphiti — the gate function."""

    def test_approved_active_not_ingested(self, tmp_path):
        """Eligible: approved + active + not ingested + file exists."""
        task = _make_task()
        fm = _make_frontmatter(approved=True, memory_status="active",
                               graphiti_ingested=False)
        note_path = tmp_path / "note.md"
        note_path.write_text("---\napproved: true\n---\nbody")

        assert should_promote_to_graphiti(task, fm, note_path) is True

    def test_not_approved(self, tmp_path):
        """Not eligible: approved=false."""
        task = _make_task()
        fm = _make_frontmatter(approved=False)
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert should_promote_to_graphiti(task, fm, note_path) is False

    def test_archived(self, tmp_path):
        """Not eligible: memory_status=archived."""
        task = _make_task()
        fm = _make_frontmatter(memory_status="archived")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert should_promote_to_graphiti(task, fm, note_path) is False

    def test_already_ingested(self, tmp_path):
        """Not eligible: graphiti_ingested=true."""
        task = _make_task()
        fm = _make_frontmatter(graphiti_ingested=True)
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert should_promote_to_graphiti(task, fm, note_path) is False

    def test_file_not_found(self, tmp_path):
        """Not eligible: vault note file doesn't exist."""
        task = _make_task()
        fm = _make_frontmatter()
        note_path = tmp_path / "nonexistent.md"

        assert should_promote_to_graphiti(task, fm, note_path) is False

    def test_draft_status(self, tmp_path):
        """Not eligible: memory_status=draft."""
        task = _make_task()
        fm = _make_frontmatter(memory_status="draft")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert should_promote_to_graphiti(task, fm, note_path) is False


class TestGetPromotionPriority:
    """get_promotion_priority — priority levels."""

    def test_normal_priority(self, tmp_path):
        """Standard approved task gets PRIORITY_NORMAL."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="low")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert get_promotion_priority(task, fm, note_path) == PRIORITY_NORMAL

    def test_high_risk_priority(self, tmp_path):
        """High-risk approved task gets PRIORITY_HIGH."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="high")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert get_promotion_priority(task, fm, note_path) == PRIORITY_HIGH

    def test_failed_task_urgent(self, tmp_path):
        """Failed task gets PRIORITY_URGENT (known failure)."""
        task = _make_task(status=TaskStatus.FAILED)
        fm = _make_frontmatter(risk="low")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert get_promotion_priority(task, fm, note_path) == PRIORITY_URGENT

    def test_blocked_when_not_eligible(self, tmp_path):
        """Not eligible returns PROMOTION_BLOCKED."""
        task = _make_task()
        fm = _make_frontmatter(approved=False)
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert get_promotion_priority(task, fm, note_path) == PROMOTION_BLOCKED

    def test_adr_gets_urgent_priority(self, tmp_path):
        """Architectural Decision Records (type=decision) get PRIORITY_URGENT."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="low")
        fm["type"] = "decision"
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert get_promotion_priority(task, fm, note_path) == PRIORITY_URGENT

    def test_adr_overrides_high_risk(self, tmp_path):
        """ADR priority overrides even high-risk classification."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="high")
        fm["type"] = "decision"
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        assert get_promotion_priority(task, fm, note_path) == PRIORITY_URGENT


class TestGetPromotionExclusions:
    """get_promotion_exclusions — reasons why not to promote."""

    def test_no_exclusions_when_eligible(self, tmp_path):
        task = _make_task()
        fm = _make_frontmatter()
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        exclusions = get_promotion_exclusions(task, fm, note_path)
        assert exclusions == []

    def test_exclusion_not_approved(self, tmp_path):
        task = _make_task()
        fm = _make_frontmatter(approved=False)
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        exclusions = get_promotion_exclusions(task, fm, note_path)
        assert any("not approved" in e for e in exclusions)

    def test_exclusion_already_ingested(self, tmp_path):
        task = _make_task()
        fm = _make_frontmatter(graphiti_ingested=True)
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        exclusions = get_promotion_exclusions(task, fm, note_path)
        assert any("already ingested" in e for e in exclusions)

    def test_exclusion_file_not_found(self, tmp_path):
        task = _make_task()
        fm = _make_frontmatter()
        note_path = tmp_path / "nonexistent.md"

        exclusions = get_promotion_exclusions(task, fm, note_path)
        assert any("not found" in e for e in exclusions)

    def test_exclusion_reject_recommendation(self, tmp_path):
        """Notes with recommendation='reject' are excluded."""
        task = _make_task()
        fm = _make_frontmatter()
        fm["recommendation"] = "reject"
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        exclusions = get_promotion_exclusions(task, fm, note_path)
        assert any("reject" in e for e in exclusions)


class TestGetPromotionMetadata:
    """get_promotion_metadata — combined metadata dict."""

    def test_metadata_eligible(self, tmp_path):
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="low")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        meta = get_promotion_metadata(task, fm, note_path)
        assert meta["eligible"] is True
        assert meta["priority"] == PRIORITY_NORMAL
        assert meta["exclusions"] == []
        assert meta["task_id"] == task.task_id
        assert meta["is_known_failure"] is False
        assert meta["needs_secondary_review"] is False

    def test_metadata_high_risk(self, tmp_path):
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="high")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        meta = get_promotion_metadata(task, fm, note_path)
        assert meta["needs_secondary_review"] is True
        assert meta["priority"] == PRIORITY_HIGH

    def test_metadata_failed_task(self, tmp_path):
        task = _make_task(status=TaskStatus.FAILED)
        fm = _make_frontmatter()
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        meta = get_promotion_metadata(task, fm, note_path)
        assert meta["is_known_failure"] is True
        assert meta["priority"] == PRIORITY_URGENT

    def test_metadata_richer_fields(self, tmp_path):
        """Metadata includes branch, files_changed, insertions, deletions, etc."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="medium")
        fm["files_changed"] = 5
        fm["insertions"] = 100
        fm["deletions"] = 20
        fm["created"] = "2026-06-26"
        fm["sources"] = ["diff.patch", "review.json"]
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        meta = get_promotion_metadata(task, fm, note_path)
        assert meta["branch_edited"] == task.task_branch
        assert meta["files_changed"] == 5
        assert meta["insertions"] == 100
        assert meta["deletions"] == 20
        assert meta["created_at"] == "2026-06-26"
        assert meta["sources"] == ["diff.patch", "review.json"]
        assert meta["risk_level"] == "medium"
        assert meta["repo"] == task.repo_name

    def test_metadata_adr_flag(self, tmp_path):
        """ADR notes (type=decision) are flagged in metadata."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="low")
        fm["type"] = "decision"
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        meta = get_promotion_metadata(task, fm, note_path)
        assert meta["is_adr"] is True
        assert meta["priority"] == PRIORITY_URGENT

    def test_metadata_not_adr(self, tmp_path):
        """Standard task reports are not flagged as ADRs."""
        task = _make_task(status=TaskStatus.PASSED)
        fm = _make_frontmatter(risk="low")
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        meta = get_promotion_metadata(task, fm, note_path)
        assert meta["is_adr"] is False


# --------------------------------------------------------------------------- #
# 2. Human firewall
# --------------------------------------------------------------------------- #


class TestHumanFirewall:
    """The human firewall rejects unapproved/archived/ingested notes."""

    def test_rejects_unapproved(self):
        from acp.memory.graphiti_client import HumanFirewallError, _check_human_firewall
        from acp.vault.frontmatter import Frontmatter

        fm = Frontmatter(
            type="task_report",
            approved=False,
            memory_status="active",
        )
        with pytest.raises(HumanFirewallError, match="not approved"):
            _check_human_firewall(fm)

    def test_rejects_archived(self):
        from acp.memory.graphiti_client import HumanFirewallError, _check_human_firewall
        from acp.vault.frontmatter import Frontmatter

        fm = Frontmatter(
            type="task_report",
            approved=True,
            memory_status="archived",
        )
        with pytest.raises(HumanFirewallError, match="archived"):
            _check_human_firewall(fm)

    def test_rejects_already_ingested(self):
        from acp.memory.graphiti_client import HumanFirewallError, _check_human_firewall
        from acp.vault.frontmatter import Frontmatter

        fm = Frontmatter(
            type="task_report",
            approved=True,
            memory_status="active",
            graphiti_ingested=True,
        )
        with pytest.raises(HumanFirewallError, match="already been ingested"):
            _check_human_firewall(fm)

    def test_passes_approved_active_not_ingested(self):
        from acp.memory.graphiti_client import _check_human_firewall
        from acp.vault.frontmatter import Frontmatter

        fm = Frontmatter(
            type="task_report",
            approved=True,
            memory_status="active",
            graphiti_ingested=False,
        )
        # Should not raise.
        _check_human_firewall(fm)


# --------------------------------------------------------------------------- #
# 3. CLI acp memory promote (dry run)
# --------------------------------------------------------------------------- #


class TestCLIMemoryPromote:
    """acp memory promote --dry-run shows eligible notes."""

    def test_dry_run_shows_eligible(self, tmp_path):
        from acp.store import TaskStore
        from acp.models import Task, TaskStatus

        # Set up vault with an approved note.
        vault_root = tmp_path / "vault"
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        task_id = "task_20260626_0001"
        _write_vault_note(vault_root, task_id, approved=True,
                          memory_status="active", graphiti_ingested=False)

        # Create task.json so the CLI can load it.
        store = TaskStore(runs_root=runs_root)
        task = Task(
            task_id=task_id,
            repo_name="test-repo",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch=f"agent/{task_id}",
            worktree_path=tmp_path / "worktree",
            user_request="Fix the auth bug",
            status=TaskStatus.PASSED,
        )
        run_dir = store.run_dir(task_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)

        # Create repo config.
        config_path = tmp_path / "test.repo.yaml"
        config_path.write_text(
            f"repo:\n"
            f"  name: test-repo\n"
            f"  path: {tmp_path / 'repo'}\n"
        )

        # Run the CLI command in dry-run mode.
        from typer.testing import CliRunner
        from acp.cli import app

        runner = CliRunner()
        result = runner.invoke(app, [
            "memory", "promote",
            "--config", str(config_path),
            "--vault-root", str(vault_root),
            "--runs-root", str(runs_root),
            "--dry-run",
        ])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "1 eligible note" in result.output or "1 eligible" in result.output
        assert task_id in result.output
        assert "no ingestion performed" in result.output

    def test_dry_run_no_eligible_notes(self, tmp_path):
        from typer.testing import CliRunner
        from acp.cli import app

        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        (vault_root / "tasks").mkdir()

        config_path = tmp_path / "test.repo.yaml"
        config_path.write_text(
            f"repo:\n"
            f"  name: test-repo\n"
            f"  path: {tmp_path / 'repo'}\n"
        )

        runner = CliRunner()
        result = runner.invoke(app, [
            "memory", "promote",
            "--config", str(config_path),
            "--vault-root", str(vault_root),
            "--dry-run",
        ])

        assert result.exit_code == 0
        assert "No eligible notes" in result.output or "No vault notes" in result.output


# --------------------------------------------------------------------------- #
# 4. Auto-promotion in auto_approve_node
# --------------------------------------------------------------------------- #


class TestAutoPromotion:
    """auto_approve_node auto-promotes when promote_reports_by_default=True."""

    def test_auto_promote_when_configured(self, tmp_path):
        """When promote_reports_by_default=True, auto_approve promotes to Graphiti."""
        from acp.graph.nodes import NodeContext, auto_approve_node
        from acp.events import EventWriter
        from acp.store import TaskStore

        cfg = MagicMock()
        cfg.review.autonomous_mode = True
        cfg.memory.promote_reports_by_default = True
        cfg.memory.graphiti_group_id = ""

        task = _make_task(status=TaskStatus.PASSED)

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir(task.task_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter(task.task_id, run_dir)
        # Establish a valid event chain so the integrity gate passes.
        events.write(EventType.TASK_CREATED, {"task_id": task.task_id})

        # Write a vault note.
        vault_note_path = tmp_path / "vault" / "tasks" / f"{task.task_id}.md"
        vault_note_path.parent.mkdir(parents=True, exist_ok=True)
        vault_note_path.write_text(
            f"---\n"
            f"type: task_report\n"
            f"task_id: {task.task_id}\n"
            f"approved: true\n"
            f"memory_status: active\n"
            f"graphiti_ingested: false\n"
            f"risk: low\n"
            f"---\n\nReport body\n"
        )

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "vault_note_path": vault_note_path,
        }

        # Mock the Graphiti ingestion (memory extra may not be installed).
        with patch(
            "acp.memory.graphiti_client.ingest_task_to_graphiti"
        ) as mock_ingest:
            mock_ingest.return_value = {
                "task_id": task.task_id,
                "episode_id": "ep-123",
                "nodes_created": 3,
                "edges_created": 2,
            }
            result = auto_approve_node(state, ctx)

        assert result["auto_approved"] is True
        assert result["memory_promoted"] is True
        mock_ingest.assert_called_once()

        # Check that MEMORY_PROMOTED event was written.
        all_events = events.read_all()
        promoted_events = [e for e in all_events if e.type.value == "memory.promoted"]
        assert len(promoted_events) == 1
        assert promoted_events[0].payload["auto_promoted"] is True

    def test_no_auto_promote_when_not_configured(self, tmp_path):
        """When promote_reports_by_default=False, no auto-promotion."""
        from acp.graph.nodes import NodeContext, auto_approve_node
        from acp.events import EventWriter
        from acp.store import TaskStore

        cfg = MagicMock()
        cfg.review.autonomous_mode = True
        cfg.memory.promote_reports_by_default = False

        task = _make_task(status=TaskStatus.PASSED)

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir(task.task_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter(task.task_id, run_dir)
        # Establish a valid event chain so the integrity gate passes.
        events.write(EventType.TASK_CREATED, {"task_id": task.task_id})

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
        }

        result = auto_approve_node(state, ctx)

        assert result["auto_approved"] is True
        assert result["memory_promoted"] is False

    def test_auto_promote_silent_on_import_error(self, tmp_path):
        """When memory extra not installed, auto-promote fails silently."""
        from acp.graph.nodes import NodeContext, auto_approve_node
        from acp.events import EventWriter
        from acp.store import TaskStore

        cfg = MagicMock()
        cfg.review.autonomous_mode = True
        cfg.memory.promote_reports_by_default = True
        cfg.memory.graphiti_group_id = ""

        task = _make_task(status=TaskStatus.PASSED)

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir(task.task_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter(task.task_id, run_dir)
        # Establish a valid event chain so the integrity gate passes.
        events.write(EventType.TASK_CREATED, {"task_id": task.task_id})

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "vault_note_path": None,  # No vault note path
        }

        result = auto_approve_node(state, ctx)

        # Approval still stands, memory not promoted.
        assert result["auto_approved"] is True
        assert result["memory_promoted"] is False


# --------------------------------------------------------------------------- #
# 5. Full Graphiti integration (skipped if no memory extra)
# --------------------------------------------------------------------------- #


@memory_skip
class TestFullGraphitiIntegration:
    """Full Graphiti integration tests (require memory extra + FalkorDB)."""

    def test_ingest_rejects_unapproved(self, tmp_path):
        """ingest_task_to_graphiti rejects unapproved notes."""
        from acp.memory.graphiti_client import (
            HumanFirewallError,
            ingest_task_to_graphiti,
        )

        task = _make_task()
        fm = _make_frontmatter(approved=False)
        note_path = tmp_path / "note.md"
        note_path.write_text("---\napproved: false\n---\nbody")

        with pytest.raises(HumanFirewallError):
            ingest_task_to_graphiti(task, fm, note_path)

    def test_ingest_rejects_already_ingested(self, tmp_path):
        """ingest_task_to_graphiti rejects already-ingested notes."""
        from acp.memory.graphiti_client import (
            HumanFirewallError,
            ingest_task_to_graphiti,
        )

        task = _make_task()
        fm = _make_frontmatter(graphiti_ingested=True)
        note_path = tmp_path / "note.md"
        note_path.write_text("content")

        with pytest.raises(HumanFirewallError):
            ingest_task_to_graphiti(task, fm, note_path)

    def test_mark_as_ingested_updates_frontmatter(self, tmp_path):
        """_mark_as_ingested flips graphiti_ingested to true."""
        from acp.memory.graphiti_client import _mark_as_ingested
        from acp.vault.frontmatter import parse_frontmatter

        note_path = tmp_path / "note.md"
        note_path.write_text(
            "---\n"
            "type: task_report\n"
            "approved: true\n"
            "memory_status: active\n"
            "graphiti_ingested: false\n"
            "---\n\nReport body\n"
        )

        _mark_as_ingested(note_path)

        content = note_path.read_text()
        fm, body = parse_frontmatter(content)
        assert fm.graphiti_ingested is True
        assert "Report body" in body
