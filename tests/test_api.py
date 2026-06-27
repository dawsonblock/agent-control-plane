"""M10 FastAPI control layer tests.

Tests the HTTP API endpoints. Uses FastAPI's TestClient (which requires
the `api` extra). Tests are skipped if FastAPI is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    FASTAPI_INSTALLED = True
except ImportError:
    FASTAPI_INSTALLED = False

api_skip = pytest.mark.skipif(
    not FASTAPI_INSTALLED,
    reason="api extra not installed (uv sync --extra api)",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_repo_config(tmp_path: Path) -> Path:
    """Create a minimal repo config for testing."""
    config_path = tmp_path / "test.repo.yaml"
    config_path.write_text(
        f"repo:\n"
        f"  name: test-repo\n"
        f"  path: {tmp_path / 'repo'}\n"
        f"  default_branch: main\n"
        f"agent:\n"
        f"  default: shell\n"
    )
    return config_path


def _make_client(tmp_path: Path):
    """Create a TestClient with a configured repo."""
    from acp.api.server import app, state

    config_path = _make_repo_config(tmp_path)
    state.set_config(str(config_path))
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Health and config
# --------------------------------------------------------------------------- #


@api_skip
class TestHealth:
    """Health and config endpoints."""

    def test_health(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_set_config(self, tmp_path):
        from acp.api.server import app, state

        config_path = _make_repo_config(tmp_path)
        client = TestClient(app)
        state._config_cache = None  # reset

        resp = client.post("/config", params={"config_path": str(config_path)})
        assert resp.status_code == 200
        assert resp.json()["config_path"] == str(config_path)

    def test_set_config_not_found(self, tmp_path):
        from acp.api.server import app, state

        client = TestClient(app)
        state._config_cache = None

        resp = client.post("/config", params={"config_path": "/nonexistent.yaml"})
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Task listing
# --------------------------------------------------------------------------- #


@api_skip
class TestTaskListing:
    """GET /tasks — list tasks."""

    def test_list_empty(self, tmp_path):
        client = _make_client(tmp_path)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        resp = client.get("/tasks", params={"runs_root": str(runs_root)})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_task(self, tmp_path):
        from acp.models import Task, TaskStatus
        from acp.store import TaskStore

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_20260626_0001")
        run_dir.mkdir(parents=True, exist_ok=True)

        task = Task(
            task_id="task_20260626_0001",
            repo_name="test-repo",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_20260626_0001",
            worktree_path=tmp_path / "worktree",
            user_request="Fix the bug",
            status=TaskStatus.PASSED,
        )
        store.save(task)

        client = _make_client(tmp_path)
        resp = client.get("/tasks", params={"runs_root": str(runs_root)})
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "task_20260626_0001"
        assert tasks[0]["status"] == "passed"


# --------------------------------------------------------------------------- #
# Task detail
# --------------------------------------------------------------------------- #


@api_skip
class TestTaskDetail:
    """GET /tasks/{task_id} — get task status."""

    def test_get_task_rejects_invalid_id(self, tmp_path):
        """Invalid task_id should return 400, not 404 or 500."""
        client = _make_client(tmp_path)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        resp = client.get(
            "/tasks/invalid_id",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 400

    def test_get_task(self, tmp_path):
        from acp.models import Task, TaskStatus
        from acp.store import TaskStore

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_20260626_0001")
        run_dir.mkdir(parents=True, exist_ok=True)

        task = Task(
            task_id="task_20260626_0001",
            repo_name="test-repo",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_20260626_0001",
            worktree_path=tmp_path / "worktree",
            user_request="Fix the bug",
            status=TaskStatus.PASSED,
        )
        store.save(task)

        client = _make_client(tmp_path)
        resp = client.get(
            "/tasks/task_20260626_0001",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "task_20260626_0001"
        assert data["status"] == "passed"

    def test_get_task_not_found(self, tmp_path):
        client = _make_client(tmp_path)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        resp = client.get(
            "/tasks/task_20260626_9999",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@api_skip
class TestEvents:
    """GET /tasks/{task_id}/events — get event log."""

    def test_get_events(self, tmp_path):
        from acp.events import EventWriter
        from acp.store import TaskStore

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_20260626_0001")
        run_dir.mkdir(parents=True, exist_ok=True)

        events = EventWriter("task_20260626_0001", run_dir)
        from acp.models import EventType

        events.write(EventType.TASK_CREATED, {"test": True})

        client = _make_client(tmp_path)
        resp = client.get(
            "/tasks/task_20260626_0001/events",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 200
        events_list = resp.json()
        assert len(events_list) >= 1
        assert events_list[0]["type"] == "task.created"

    def test_get_events_not_found(self, tmp_path):
        client = _make_client(tmp_path)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        resp = client.get(
            "/tasks/task_20260626_9999/events",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@api_skip
class TestReport:
    """GET /tasks/{task_id}/report — get report content."""

    def test_get_report(self, tmp_path):
        from acp.store import TaskStore

        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_20260626_0001")
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "report.md").write_text("# Report\nFixed the bug.")

        client = _make_client(tmp_path)
        resp = client.get(
            "/tasks/task_20260626_0001/report",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "Fixed the bug" in data["report"]

    def test_get_report_not_found(self, tmp_path):
        client = _make_client(tmp_path)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        resp = client.get(
            "/tasks/task_20260626_9999/report",
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Approve / Reject
# --------------------------------------------------------------------------- #


@api_skip
class TestApproveReject:
    """POST /tasks/{task_id}/approve and /reject."""

    def _setup_task_and_note(self, tmp_path, status="passed"):
        from acp.models import Task, TaskStatus
        from acp.store import TaskStore

        runs_root = tmp_path / "runs"
        vault_root = tmp_path / "vault"
        runs_root.mkdir()

        store = TaskStore(runs_root=runs_root)
        run_dir = store.run_dir("task_20260626_0001")
        run_dir.mkdir(parents=True, exist_ok=True)

        task = Task(
            task_id="task_20260626_0001",
            repo_name="test-repo",
            repo_path=tmp_path / "repo",
            base_branch="main",
            task_branch="agent/task_20260626_0001",
            worktree_path=tmp_path / "worktree",
            user_request="Fix the bug",
            status=TaskStatus(status),
        )
        store.save(task)

        # Write a vault note
        note_path = vault_root / "tasks" / "task_20260626_0001.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\n"
            "type: task_report\n"
            "task_id: task_20260626_0001\n"
            "approved: false\n"
            "memory_status: draft\n"
            "graphiti_ingested: false\n"
            "risk: low\n"
            "recommendation: merge\n"
            "status: passed\n"
            "---\n\nReport body\n"
        )

        return runs_root, vault_root

    def test_approve_task(self, tmp_path):
        runs_root, vault_root = self._setup_task_and_note(tmp_path)
        client = _make_client(tmp_path)

        resp = client.post(
            "/tasks/task_20260626_0001/approve",
            json={"approver": "alice"},
            params={"runs_root": str(runs_root), "vault_root": str(vault_root)},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_approve_not_found(self, tmp_path):
        client = _make_client(tmp_path)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        resp = client.post(
            "/tasks/task_20260626_9999/approve",
            json={"approver": "alice"},
            params={"runs_root": str(runs_root)},
        )
        assert resp.status_code == 404

    def test_reject_task(self, tmp_path):
        runs_root, vault_root = self._setup_task_and_note(tmp_path)
        client = _make_client(tmp_path)

        resp = client.post(
            "/tasks/task_20260626_0001/reject",
            json={"rejecter": "bob"},
            params={"runs_root": str(runs_root), "vault_root": str(vault_root)},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
