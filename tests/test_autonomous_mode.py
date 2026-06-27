"""v0.6.0 Autonomous Mode tests.

Tests the autonomous mode feature:

  1. Config fields (autonomous_mode, auto_merge,
     dynamic_test_generation, repair_repeat_breaker)
  2. GitOps merge module (merge_to_base, conflict handling)
  3. Auto-approve node (writes auto.approved event)
  4. Auto-merge node (writes auto.merged event, handles conflicts)
  5. Graph routing (write_report → auto_approve → auto_merge → done)
  6. Circuit breaker (stops repair loop on repeated failures)
  7. TESTS_MISSING repair prompt (dynamic test generation)
  8. Evidence classification (auto.approved/auto.merged as post-run)
  9. derive_status_from_events handles auto.approved
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from git import Repo

from acp.config import AgentSection, RepoConfig, RepoSection, ReviewSection
from acp.models import EventType, Task, TaskStatus

# --------------------------------------------------------------------------- #
# 1. Config fields
# --------------------------------------------------------------------------- #


class TestAutonomousConfig:
    """Config fields for autonomous mode."""

    def test_autonomous_mode_defaults_false(self):
        cfg = ReviewSection()
        assert cfg.autonomous_mode is False

    def test_auto_merge_defaults_false(self):
        cfg = ReviewSection()
        assert cfg.auto_merge is False

    def test_dynamic_test_generation_defaults_true(self):
        cfg = AgentSection()
        assert cfg.dynamic_test_generation is True

    def test_repair_repeat_breaker_defaults_3(self):
        cfg = AgentSection()
        assert cfg.repair_repeat_breaker == 3

    def test_autonomous_mode_can_be_enabled(self):
        cfg = ReviewSection(autonomous_mode=True)
        assert cfg.autonomous_mode is True

    def test_auto_merge_can_be_enabled(self):
        cfg = ReviewSection(auto_merge=True)
        assert cfg.auto_merge is True


# --------------------------------------------------------------------------- #
# 2. GitOps merge module
# --------------------------------------------------------------------------- #


class TestGitOpsMerge:
    """merge_to_base merges task branch into default branch."""

    def _setup_repo(self, tmp_path: Path) -> tuple[Path, str]:
        """Create a test repo with a base branch and a task branch."""
        repo_path = tmp_path / "repo"
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")

        # Base commit on main.
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base commit")
        repo.git.branch("-M", "main")

        # Task branch with a new file.
        repo.git.checkout("-b", "agent/task_001")
        (repo_path / "feature.py").write_text("print('hello')\n")
        repo.git.add(".")
        repo.git.commit("-m", "add feature")

        # Back to main.
        repo.git.checkout("main")

        return repo_path, "agent/task_001"

    def test_merge_to_base_success(self, tmp_path):
        """merge_to_base merges the task branch into main."""
        from acp.gitops.merge import merge_to_base

        repo_path, task_branch = self._setup_repo(tmp_path)

        merge_sha = merge_to_base(repo_path, "agent/task_001", "main")

        # Verify the merge happened.
        repo = Repo(str(repo_path))
        assert repo.active_branch.name == "main"
        assert repo.head.commit.hexsha == merge_sha

        # The feature file should be on main now.
        assert (repo_path / "feature.py").exists()

    def test_merge_to_base_missing_base_branch(self, tmp_path):
        """merge_to_base raises if base branch doesn't exist."""
        from acp.gitops.merge import merge_to_base

        repo_path, _ = self._setup_repo(tmp_path)

        with pytest.raises(RuntimeError, match="base branch"):
            merge_to_base(repo_path, "agent/task_001", "nonexistent")

    def test_merge_to_base_missing_task_branch(self, tmp_path):
        """merge_to_base raises if task branch doesn't exist."""
        from acp.gitops.merge import merge_to_base

        repo_path, _ = self._setup_repo(tmp_path)

        with pytest.raises(RuntimeError, match="task branch"):
            merge_to_base(repo_path, "nonexistent", "main")

    def test_merge_to_base_conflict(self, tmp_path):
        """merge_to_base aborts on conflict and raises."""
        from acp.gitops.merge import merge_to_base

        repo_path, _ = self._setup_repo(tmp_path)

        # Create a conflicting change on main.
        repo = Repo(str(repo_path))
        (repo_path / "feature.py").write_text("print('conflict')\n")
        repo.git.add(".")
        repo.git.commit("-m", "conflicting change on main")

        # Now merge should fail.
        with pytest.raises(RuntimeError, match="Auto-merge failed"):
            merge_to_base(repo_path, "agent/task_001", "main")

        # Verify main was not modified by the failed merge.
        assert (repo_path / "feature.py").read_text() == "print('conflict')\n"


# --------------------------------------------------------------------------- #
# 3. Auto-approve node
# --------------------------------------------------------------------------- #


class TestAutoApproveNode:
    """auto_approve_node writes auto.approved event."""

    def _make_config(self, **kwargs) -> RepoConfig:
        review = ReviewSection(
            autonomous_mode=kwargs.get("autonomous_mode", False),
            auto_merge=kwargs.get("auto_merge", False),
        )
        return RepoConfig(
            repo=RepoSection(
                name="test",
                path=Path("/tmp/test"),
                default_branch="main",
            ),
            review=review,
        )

    async def test_auto_approve_writes_event(self, tmp_path):
        """auto_approve_node writes auto.approved when enabled."""
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_approve_node
        from acp.store import TaskStore

        cfg = self._make_config(autonomous_mode=True)
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test request",
            status=TaskStatus.PASSED,
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        # A real run has a full event chain by auto-approve time; establish a
        # valid chain so the integrity gate passes and the approval proceeds.
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})

        ctx = NodeContext(store=store, events=events)
        state = {"config": cfg, "task": task}

        result = await auto_approve_node(state, ctx)

        assert result.get("auto_approved") is True
        assert result.get("status") == TaskStatus.APPROVED

        all_events = events.read_all()
        assert any(e.type == EventType.AUTO_APPROVED for e in all_events)
        approved = [e for e in all_events if e.type == EventType.AUTO_APPROVED]
        assert approved[0].payload["approver"] == "ACP-Autonomous-Bot"

    async def test_auto_approve_noop_when_disabled(self, tmp_path):
        """auto_approve_node is a no-op when autonomous_mode is False."""
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_approve_node
        from acp.store import TaskStore

        cfg = self._make_config(autonomous_mode=False)
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test request",
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        events = EventWriter("task_001", run_dir)

        ctx = NodeContext(store=store, events=events)
        state = {"config": cfg, "task": task}

        result = await auto_approve_node(state, ctx)

        assert result == {}
        assert len(events.read_all()) == 0

    async def test_auto_approve_refused_on_broken_event_chain(
        self,
        tmp_path,
    ):
        """auto_approve_node refuses when the event chain is empty/broken.

        Integrity gate: a task with no tamper-proof audit trail may not
        be auto-approved. An empty event chain fails verify_event_chain
        and the task is downgraded to NEEDS_REVIEW.
        """
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_approve_node
        from acp.store import TaskStore

        cfg = self._make_config(autonomous_mode=True)
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test request",
            status=TaskStatus.PASSED,
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        # No events written — empty chain fails verify_event_chain.

        ctx = NodeContext(store=store, events=events)
        state = {"config": cfg, "task": task}

        result = await auto_approve_node(state, ctx)

        assert result.get("auto_approved") is False
        assert result.get("status") == TaskStatus.NEEDS_REVIEW
        assert "event chain verification" in result.get("error", "")


# --------------------------------------------------------------------------- #
# 4. Auto-merge node
# --------------------------------------------------------------------------- #


class TestAutoMergeNode:
    """auto_merge_node merges and writes auto.merged event."""

    async def test_auto_merge_writes_event(self, tmp_path):
        """auto_merge_node writes auto.merged on success."""
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_merge_node
        from acp.store import TaskStore

        # Set up a real repo.
        repo_path = tmp_path / "repo"
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base")
        repo.git.branch("-M", "main")
        repo.git.checkout("-b", "agent/task_001")
        (repo_path / "feature.py").write_text("print('hi')\n")
        repo.git.add(".")
        repo.git.commit("-m", "feature")
        repo.git.checkout("main")

        cfg = RepoConfig(
            repo=RepoSection(
                name="test",
                path=repo_path,
                default_branch="main",
            ),
            review=ReviewSection(
                autonomous_mode=True,
                auto_merge=True,
            ),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=repo_path,
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        # A real run has a full event chain by auto-merge time; establish a
        # valid chain so the integrity gate passes and the merge proceeds.
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "repo_path": repo_path,
        }

        result = await auto_merge_node(state, ctx)

        assert result.get("auto_merged") is True
        assert "merge_commit_sha" in result

        all_events = events.read_all()
        assert any(e.type == EventType.AUTO_MERGED for e in all_events)

    async def test_auto_merge_noop_when_disabled(self, tmp_path):
        """auto_merge_node is a no-op when auto_merge is False."""
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_merge_node
        from acp.store import TaskStore

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=Path("/tmp")),
            review=ReviewSection(auto_merge=False),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=Path("/tmp"),
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        events = EventWriter("task_001", run_dir)

        ctx = NodeContext(store=store, events=events)
        state = {"config": cfg, "task": task}

        result = await auto_merge_node(state, ctx)
        assert result == {}

    async def test_auto_merge_conflict_downgrades_to_needs_review(
        self,
        tmp_path,
    ):
        """auto_merge_node downgrades to NEEDS_REVIEW on conflict."""
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_merge_node
        from acp.store import TaskStore

        # Set up a repo with a conflict.
        repo_path = tmp_path / "repo"
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "file.py").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base")
        repo.git.branch("-M", "main")

        # Task branch changes file.py.
        repo.git.checkout("-b", "agent/task_001")
        (repo_path / "file.py").write_text("# task\n")
        repo.git.add(".")
        repo.git.commit("-m", "task change")

        # Main changes the same file.
        repo.git.checkout("main")
        (repo_path / "file.py").write_text("# main\n")
        repo.git.add(".")
        repo.git.commit("-m", "main change")

        cfg = RepoConfig(
            repo=RepoSection(
                name="test",
                path=repo_path,
                default_branch="main",
            ),
            review=ReviewSection(
                autonomous_mode=True,
                auto_merge=True,
            ),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=repo_path,
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
            status=TaskStatus.APPROVED,
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        # Establish a valid chain so the integrity gate passes and the
        # conflict is actually exercised (not short-circuited).
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "repo_path": repo_path,
        }

        result = await auto_merge_node(state, ctx)

        assert result.get("auto_merged") is False
        assert result.get("status") == TaskStatus.NEEDS_REVIEW

    async def test_auto_merge_refused_on_high_risk(self, tmp_path):
        """auto_merge_node refuses when review risk exceeds auto_merge_max_risk.

        Human firewall: a HIGH-risk change (database, secrets, auth) may
        not be auto-merged to the default branch — a human must click
        approved: true first. The refusal writes an auto.merge.refused
        event and downgrades to NEEDS_REVIEW.
        """
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_merge_node
        from acp.models import Recommendation, ReviewResult, RiskLevel
        from acp.store import TaskStore

        repo_path = tmp_path / "repo"
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base")
        repo.git.branch("-M", "main")
        repo.git.checkout("-b", "agent/task_001")
        (repo_path / "feature.py").write_text("print('hi')\n")
        repo.git.add(".")
        repo.git.commit("-m", "feature")
        repo.git.checkout("main")

        cfg = RepoConfig(
            repo=RepoSection(
                name="test",
                path=repo_path,
                default_branch="main",
            ),
            review=ReviewSection(
                autonomous_mode=True,
                auto_merge=True,
                auto_merge_max_risk=RiskLevel.MEDIUM,
            ),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=repo_path,
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
            status=TaskStatus.APPROVED,
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})

        review = ReviewResult(
            risk=RiskLevel.HIGH,
            recommendation=Recommendation.MERGE,
            concerns=["high-risk change"],
            hard_block=False,
        )

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "repo_path": repo_path,
            "review_result": review,
        }

        result = await auto_merge_node(state, ctx)

        assert result.get("auto_merged") is False
        assert result.get("status") == TaskStatus.NEEDS_REVIEW
        assert "exceeds auto_merge_max_risk" in result.get("error", "")

        # The refusal is recorded in the event log.
        all_events = events.read_all()
        refused = [e for e in all_events if e.type == EventType.AUTO_MERGE_REFUSED]
        assert len(refused) == 1
        assert refused[0].payload["reason"] == "risk_exceeds_max"
        assert refused[0].payload["review_risk"] == "high"

        # And no auto.merged event was written.
        merged = [e for e in all_events if e.type == EventType.AUTO_MERGED]
        assert len(merged) == 0

        # The merge must NOT have happened — feature.py is not on main.
        repo = Repo(str(repo_path))
        repo.git.checkout("main")
        assert not (repo_path / "feature.py").exists()

    async def test_auto_merge_allowed_when_risk_at_ceiling(self, tmp_path):
        """auto_merge proceeds when review risk equals auto_merge_max_risk.

        MEDIUM risk with auto_merge_max_risk=MEDIUM is allowed (not
        strictly above the ceiling). This confirms the boundary.
        """
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_merge_node
        from acp.models import Recommendation, ReviewResult, RiskLevel
        from acp.store import TaskStore

        repo_path = tmp_path / "repo"
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base")
        repo.git.branch("-M", "main")
        repo.git.checkout("-b", "agent/task_001")
        (repo_path / "feature.py").write_text("print('hi')\n")
        repo.git.add(".")
        repo.git.commit("-m", "feature")
        repo.git.checkout("main")

        cfg = RepoConfig(
            repo=RepoSection(
                name="test",
                path=repo_path,
                default_branch="main",
            ),
            review=ReviewSection(
                autonomous_mode=True,
                auto_merge=True,
                auto_merge_max_risk=RiskLevel.MEDIUM,
            ),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=repo_path,
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
            status=TaskStatus.APPROVED,
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})

        review = ReviewResult(
            risk=RiskLevel.MEDIUM,
            recommendation=Recommendation.MERGE,
            concerns=[],
            hard_block=False,
        )

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "repo_path": repo_path,
            "review_result": review,
        }

        result = await auto_merge_node(state, ctx)

        assert result.get("auto_merged") is True
        all_events = events.read_all()
        assert any(e.type == EventType.AUTO_MERGED for e in all_events)

    async def test_auto_merge_refused_on_broken_event_chain(self, tmp_path):
        """auto_merge_node refuses when the event chain is tampered/broken.

        Integrity gate: a task with no tamper-proof audit trail may not
        reach the default branch autonomously. We corrupt the chain by
        rewriting an event's hash so verify_event_chain fails.
        """
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, auto_merge_node
        from acp.models import Recommendation, ReviewResult, RiskLevel
        from acp.store import TaskStore

        repo_path = tmp_path / "repo"
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base")
        repo.git.branch("-M", "main")
        repo.git.checkout("-b", "agent/task_001")
        (repo_path / "feature.py").write_text("print('hi')\n")
        repo.git.add(".")
        repo.git.commit("-m", "feature")
        repo.git.checkout("main")

        cfg = RepoConfig(
            repo=RepoSection(
                name="test",
                path=repo_path,
                default_branch="main",
            ),
            review=ReviewSection(
                autonomous_mode=True,
                auto_merge=True,
            ),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=repo_path,
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
            status=TaskStatus.APPROVED,
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})

        # Corrupt the event log: rewrite the hash so the chain breaks.
        log_path = run_dir / "events.jsonl"
        lines = log_path.read_text().splitlines()
        import json as _json

        first = _json.loads(lines[0])
        first["hash"] = "0" * 64  # bogus hash
        log_path.write_text(_json.dumps(first) + "\n")

        review = ReviewResult(
            risk=RiskLevel.LOW,
            recommendation=Recommendation.MERGE,
            concerns=[],
            hard_block=False,
        )

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "repo_path": repo_path,
            "review_result": review,
        }

        result = await auto_merge_node(state, ctx)

        assert result.get("auto_merged") is False
        assert result.get("status") == TaskStatus.NEEDS_REVIEW
        assert "event chain verification failed" in result.get("error", "")

        all_events = events.read_all()
        refused = [e for e in all_events if e.type == EventType.AUTO_MERGE_REFUSED]
        # The refusal event is appended after the corrupted line.
        assert any(r.payload["reason"] == "event_chain_broken" for r in refused)
        merged = [e for e in all_events if e.type == EventType.AUTO_MERGED]
        assert len(merged) == 0

        # The merge must NOT have happened.
        repo = Repo(str(repo_path))
        repo.git.checkout("main")
        assert not (repo_path / "feature.py").exists()


# --------------------------------------------------------------------------- #
# 5. Graph routing
# --------------------------------------------------------------------------- #


class TestGraphRouting:
    """Routing functions handle autonomous mode."""

    def test_route_after_write_report_autonomous(self):
        from acp.graph.workflow import _route_after_write_report

        cfg = MagicMock()
        cfg.review.autonomous_mode = True
        state = {"status": TaskStatus.PASSED, "config": cfg}
        assert _route_after_write_report(state) == "auto_approve"

    def test_route_after_write_report_non_autonomous(self):
        from acp.graph.workflow import _route_after_write_report

        cfg = MagicMock()
        cfg.review.autonomous_mode = False
        state = {"status": TaskStatus.PASSED, "config": cfg}
        assert _route_after_write_report(state) == "done"

    def test_route_after_auto_approve_to_merge(self):
        from acp.graph.workflow import _route_after_auto_approve

        cfg = MagicMock()
        cfg.review.auto_merge = True
        state = {
            "status": TaskStatus.APPROVED,
            "config": cfg,
        }
        assert _route_after_auto_approve(state) == "auto_merge"

    def test_route_after_auto_approve_to_done(self):
        from acp.graph.workflow import _route_after_auto_approve

        cfg = MagicMock()
        cfg.review.auto_merge = False
        state = {
            "status": TaskStatus.APPROVED,
            "config": cfg,
        }
        assert _route_after_auto_approve(state) == "done"

    def test_route_after_auto_merge_success(self):
        from acp.graph.workflow import _route_after_auto_merge

        state = {"status": TaskStatus.APPROVED}
        assert _route_after_auto_merge(state) == "done"

    def test_route_after_auto_merge_conflict(self):
        from acp.graph.workflow import _route_after_auto_merge

        state = {"status": TaskStatus.NEEDS_REVIEW}
        assert _route_after_auto_merge(state) == "needs_review"

    def test_route_after_review_tests_missing(self):
        """review_diff routes to repair_plan when TESTS_MISSING is flagged."""
        from acp.graph.workflow import _route_after_review

        cfg = MagicMock()
        cfg.agent.dynamic_test_generation = True
        cfg.agent.max_repair_attempts = 5
        cfg.agent.repair_repeat_breaker = 0

        review = MagicMock()
        review.concerns = [
            "Behavior changed but no test files were modified or added",
        ]

        state = {
            "config": cfg,
            "review_result": review,
            "repair_attempts": 0,
        }
        assert _route_after_review(state) == "repair_plan"

    def test_route_after_review_no_tests_missing(self):
        """review_diff routes to write_report when TESTS_MISSING is not flagged."""
        from acp.graph.workflow import _route_after_review

        cfg = MagicMock()
        cfg.agent.dynamic_test_generation = True
        cfg.agent.max_repair_attempts = 5

        review = MagicMock()
        review.concerns = ["Some other concern"]

        state = {
            "config": cfg,
            "review_result": review,
            "repair_attempts": 0,
        }
        assert _route_after_review(state) == "write_report"

    def test_route_after_review_tests_missing_disabled(self):
        """review_diff routes to write_report when dynamic_test_generation is False."""
        from acp.graph.workflow import _route_after_review

        cfg = MagicMock()
        cfg.agent.dynamic_test_generation = False
        cfg.agent.max_repair_attempts = 5

        review = MagicMock()
        review.concerns = [
            "Behavior changed but no test files were modified or added",
        ]

        state = {
            "config": cfg,
            "review_result": review,
            "repair_attempts": 0,
        }
        assert _route_after_review(state) == "write_report"

    def test_route_after_review_tests_missing_exhausted(self):
        """review_diff routes to write_report when repair attempts are exhausted."""
        from acp.graph.workflow import _route_after_review

        cfg = MagicMock()
        cfg.agent.dynamic_test_generation = True
        cfg.agent.max_repair_attempts = 2
        cfg.agent.repair_repeat_breaker = 0

        review = MagicMock()
        review.concerns = [
            "Behavior changed but no test files were modified or added",
        ]

        state = {
            "config": cfg,
            "review_result": review,
            "repair_attempts": 2,  # exhausted
        }
        assert _route_after_review(state) == "write_report"


# --------------------------------------------------------------------------- #
# 6. Circuit breaker
# --------------------------------------------------------------------------- #


class TestCircuitBreaker:
    """Circuit breaker stops repair loop on repeated failures."""

    def test_circuit_breaker_triggers_on_repeat(self):
        """When the same fingerprint repeats N times, stop repairing."""
        from acp.graph.workflow import _route_after_tests
        from acp.models import CommandResult

        cfg = MagicMock()
        cfg.agent.max_repair_attempts = 10  # high cap
        cfg.agent.repair_repeat_breaker = 3  # break after 3 repeats

        # Simulate a failing command.
        failing_result = CommandResult(
            command="pytest",
            cwd=Path("/tmp"),
            exit_code=1,
            stdout_path=Path("/tmp/out"),
            stderr_path=Path("/tmp/err"),
            duration_seconds=1.0,
            skipped=False,
        )

        # 3 identical fingerprints → breaker triggers.
        state = {
            "config": cfg,
            "command_results": [failing_result],
            "repair_attempts": 3,
            "repair_fingerprints": ["abc123", "abc123", "abc123"],
        }
        assert _route_after_tests(state) == "capture_diff"

    def test_circuit_breaker_does_not_trigger_on_varied(self):
        """Different fingerprints don't trigger the breaker."""
        from acp.graph.workflow import _route_after_tests
        from acp.models import CommandResult

        cfg = MagicMock()
        cfg.agent.max_repair_attempts = 10
        cfg.agent.repair_repeat_breaker = 3

        failing_result = CommandResult(
            command="pytest",
            cwd=Path("/tmp"),
            exit_code=1,
            stdout_path=Path("/tmp/out"),
            stderr_path=Path("/tmp/err"),
            duration_seconds=1.0,
            skipped=False,
        )

        state = {
            "config": cfg,
            "command_results": [failing_result],
            "repair_attempts": 3,
            "repair_fingerprints": ["abc", "def", "ghi"],
        }
        assert _route_after_tests(state) == "repair_plan"

    def test_circuit_breaker_disabled(self):
        """When repair_repeat_breaker=0, never break."""
        from acp.graph.workflow import _route_after_tests
        from acp.models import CommandResult

        cfg = MagicMock()
        cfg.agent.max_repair_attempts = 10
        cfg.agent.repair_repeat_breaker = 0

        failing_result = CommandResult(
            command="pytest",
            cwd=Path("/tmp"),
            exit_code=1,
            stdout_path=Path("/tmp/out"),
            stderr_path=Path("/tmp/err"),
            duration_seconds=1.0,
            skipped=False,
        )

        state = {
            "config": cfg,
            "command_results": [failing_result],
            "repair_attempts": 5,
            "repair_fingerprints": ["x"] * 5,
        }
        assert _route_after_tests(state) == "repair_plan"


# --------------------------------------------------------------------------- #
# 7. TESTS_MISSING repair prompt
# --------------------------------------------------------------------------- #


class TestTestsMissingPrompt:
    """Repair prompt includes test-writing instructions when TESTS_MISSING."""

    def test_write_repair_prompt_tests_missing(self, tmp_path):
        from acp.agents.base import write_repair_prompt
        from acp.config import RepoConfig, RepoSection

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path),
        )
        prompt_path = write_repair_prompt(
            original_request="add a feature",
            worktree_path=tmp_path / "worktree",
            artifact_dir=tmp_path / "artifacts",
            repo_config=cfg,
            failures=[],
            attempt=1,
            max_attempts=3,
            tests_missing=True,
        )

        content = prompt_path.read_text()
        assert "write" in content.lower()
        assert "test" in content.lower()
        assert "unit tests" in content.lower()

    def test_write_repair_prompt_no_tests_missing(self, tmp_path):
        from acp.agents.base import write_repair_prompt
        from acp.config import RepoConfig, RepoSection

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path),
        )
        prompt_path = write_repair_prompt(
            original_request="add a feature",
            worktree_path=tmp_path / "worktree",
            artifact_dir=tmp_path / "artifacts",
            repo_config=cfg,
            failures=[
                {
                    "command": "pytest",
                    "exit_code": 1,
                    "stdout": "FAIL",
                    "stderr": "",
                }
            ],
            attempt=1,
            max_attempts=3,
            tests_missing=False,
        )

        content = prompt_path.read_text()
        assert "Fix the root cause" in content
        assert "Do NOT delete, skip, or weaken" in content

    def test_write_missing_tests_prompt_directly(self, tmp_path):
        """write_missing_tests_prompt produces a dedicated test-gen prompt."""
        from acp.agents.base import write_missing_tests_prompt
        from acp.config import RepoConfig, RepoSection

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path),
        )
        prompt_path = write_missing_tests_prompt(
            original_request="add a feature",
            worktree_path=tmp_path / "worktree",
            artifact_dir=tmp_path / "artifacts",
            repo_config=cfg,
            failures=[],
            attempt=1,
            max_attempts=3,
        )

        content = prompt_path.read_text()
        assert "Behavior was modified but no tests were found" in content
        assert "Write unit tests" in content
        assert "Test generation attempt" in content

    async def test_tests_missing_event_written(self, tmp_path):
        """repair_plan_node writes TEST_GENERATION_ATTEMPTED when tests_missing."""
        from acp.config import (
            AgentSection,
            RepoConfig,
            RepoSection,
        )
        from acp.events import EventWriter
        from acp.graph.nodes import NodeContext, repair_plan_node
        from acp.models import Task
        from acp.store import TaskStore

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            agent=AgentSection(
                dynamic_test_generation=True,
                max_repair_attempts=3,
            ),
        )
        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
        )

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_001")
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save(task)
        events = EventWriter("task_001", run_dir)

        # A review result with TESTS_MISSING concern.
        review = MagicMock()
        review.concerns = [
            "tests_missing: Behavior changed but no test files were modified",
        ]

        ctx = NodeContext(store=store, events=events)
        state = {
            "config": cfg,
            "task": task,
            "user_request": "test",
            "worktree_path": tmp_path / "worktree",
            "artifacts_dir": run_dir / "artifacts",
            "command_results": [],
            "repair_attempts": 0,
            "repair_history": [],
            "repair_fingerprints": [],
            "review_result": review,
        }

        await repair_plan_node(state, ctx)

        all_events = events.read_all()
        types = [e.type for e in all_events]
        assert EventType.REPAIR_ATTEMPTED in types
        assert EventType.TEST_GENERATION_ATTEMPTED in types


# --------------------------------------------------------------------------- #
# 8. Evidence classification
# --------------------------------------------------------------------------- #


class TestAutoEventEvidenceClassification:
    """auto.approved and auto.merged are post-run events."""

    def test_auto_approved_does_not_break_verify(self, tmp_path):
        """auto.approved after evidence.finalized doesn't break verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "test.txt").write_text("test")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})
        events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": "acp-test",
                "executor": {
                    "network_policy": "locked_down",
                    "clone_mode": True,
                },
            },
        )
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(
            EventType.EVIDENCE_FINALIZED,
            {
                "artifact_content_hash": real_hash,
            },
        )
        report_hash = _sha256_file(
            artifacts_dir / "final_report.md",
        )
        events.write(
            EventType.EVIDENCE_REPORT_BOUND,
            {
                "report_hash": report_hash,
            },
        )
        # auto.approved AFTER finalization.
        events.write(
            EventType.AUTO_APPROVED,
            {
                "approver": "ACP-Autonomous-Bot",
            },
        )

        manifest = build_evidence_manifest(
            run_dir=run_dir,
            events_writer=events,
        )
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True

    def test_auto_merged_does_not_break_verify(self, tmp_path):
        """auto.merged after evidence.finalized doesn't break verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "test.txt").write_text("test")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_001"})
        events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": "acp-test",
                "executor": {
                    "network_policy": "locked_down",
                    "clone_mode": True,
                },
            },
        )
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(
            EventType.EVIDENCE_FINALIZED,
            {
                "artifact_content_hash": real_hash,
            },
        )
        report_hash = _sha256_file(
            artifacts_dir / "final_report.md",
        )
        events.write(
            EventType.EVIDENCE_REPORT_BOUND,
            {
                "report_hash": report_hash,
            },
        )
        events.write(
            EventType.AUTO_APPROVED,
            {
                "approver": "ACP-Autonomous-Bot",
            },
        )
        events.write(
            EventType.AUTO_MERGED,
            {
                "merge_commit_sha": "abc123",
            },
        )

        manifest = build_evidence_manifest(
            run_dir=run_dir,
            events_writer=events,
        )
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True


# --------------------------------------------------------------------------- #
# 9. derive_status_from_events handles auto.approved
# --------------------------------------------------------------------------- #


class TestDeriveStatusAutoApproved:
    """derive_status_from_events treats auto.approved as approved."""

    def test_auto_approved_maps_to_approved(self):
        from acp.evidence.manifest import derive_status_from_events
        from acp.models import Event

        events = [
            Event(
                event_id="evt_001",
                task_id="t1",
                type=EventType.TASK_CREATED,
                prev_hash="GENESIS",
                hash="h1",
            ),
            Event(
                event_id="evt_002",
                task_id="t1",
                type=EventType.TASK_COMPLETED,
                prev_hash="h1",
                hash="h2",
            ),
            Event(
                event_id="evt_003",
                task_id="t1",
                type=EventType.AUTO_APPROVED,
                prev_hash="h2",
                hash="h3",
            ),
        ]
        assert derive_status_from_events(events) == "approved"


# --------------------------------------------------------------------------- #
# 10. Vault note rendering recognizes AUTO_APPROVED
# --------------------------------------------------------------------------- #


class TestVaultNoteAutoApproved:
    """rerender_vault_note sets approved=true for auto.approved."""

    def test_vault_note_shows_approved_for_auto_approved(self, tmp_path):
        """Vault note frontmatter has approved=true when auto.approved exists."""
        from acp.events import EventWriter
        from acp.gitops.diff import DiffCapture
        from acp.models import (
            Recommendation,
            ReviewResult,
            RiskLevel,
            Task,
        )
        from acp.vault.obsidian_writer import rerender_vault_note

        task = Task(
            task_id="task_001",
            repo_name="test",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_001",
            worktree_path=tmp_path / "worktree",
            user_request="test",
            status=TaskStatus.APPROVED,
        )

        review = ReviewResult(
            risk=RiskLevel.LOW,
            recommendation=Recommendation.MERGE,
            changed_files=["feature.py"],
            concerns=[],
            summary="ok",
        )
        diff = DiffCapture(
            patch="",
            stat="",
            changed_files=["feature.py"],
            insertions=5,
            deletions=0,
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        events_writer = EventWriter("task_001", run_dir)
        events_writer.write(EventType.TASK_CREATED, {})
        events_writer.write(
            EventType.AUTO_APPROVED,
            {
                "approver": "ACP-Autonomous-Bot",
            },
        )
        all_events = events_writer.read_all()

        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        note_path = vault_root / "task_001.md"

        rerender_vault_note(
            note_path=note_path,
            report_body="# Report\n",
            task=task,
            review=review,
            diff=diff,
            events=all_events,
            vault_root=vault_root,
        )

        content = note_path.read_text()
        assert "approved: true" in content
        assert "auto_approved" in content
        assert "ACP-Autonomous-Bot" in content
