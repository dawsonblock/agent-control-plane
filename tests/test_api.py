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


# --------------------------------------------------------------------------- #
# Bearer token auth middleware
# --------------------------------------------------------------------------- #


@api_skip
class TestBearerTokenAuth:
    """Tests for the bearer token authentication middleware."""

    def test_no_token_auth_disabled(self, tmp_path):
        """When no token is configured, all endpoints are accessible."""
        from acp.api.server import app, set_api_token, state

        set_api_token(None)
        config_path = _make_repo_config(tmp_path)
        state.set_config(str(config_path))
        client = TestClient(app)

        resp = client.get("/health")
        assert resp.status_code == 200

        resp = client.get("/tasks")
        assert resp.status_code == 200

    def test_health_always_public(self, tmp_path):
        """The /health endpoint is always accessible, even with auth on."""
        from acp.api.server import app, set_api_token, state

        set_api_token("secret-token-123")
        config_path = _make_repo_config(tmp_path)
        state.set_config(str(config_path))
        try:
            client = TestClient(app)

            resp = client.get("/health")
            assert resp.status_code == 200
        finally:
            set_api_token(None)

    def test_missing_auth_header(self, tmp_path):
        """Protected endpoints return 401 without an Authorization header."""
        from acp.api.server import app, set_api_token, state

        set_api_token("secret-token-123")
        config_path = _make_repo_config(tmp_path)
        state.set_config(str(config_path))
        try:
            client = TestClient(app)

            resp = client.get("/tasks")
            assert resp.status_code == 401
            assert "Authorization" in resp.json()["detail"]
        finally:
            set_api_token(None)

    def test_wrong_token(self, tmp_path):
        """Protected endpoints return 403 with an incorrect token."""
        from acp.api.server import app, set_api_token, state

        set_api_token("secret-token-123")
        config_path = _make_repo_config(tmp_path)
        state.set_config(str(config_path))
        try:
            client = TestClient(app)

            resp = client.get("/tasks", headers={"Authorization": "Bearer wrong-token"})
            assert resp.status_code == 403
        finally:
            set_api_token(None)

    def test_valid_token(self, tmp_path):
        """Protected endpoints return 200 with the correct token."""
        from acp.api.server import app, set_api_token, state

        set_api_token("secret-token-123")
        config_path = _make_repo_config(tmp_path)
        state.set_config(str(config_path))
        try:
            client = TestClient(app)

            resp = client.get("/tasks", headers={"Authorization": "Bearer secret-token-123"})
            assert resp.status_code == 200
        finally:
            set_api_token(None)

    def test_malformed_auth_header(self, tmp_path):
        """Non-Bearer auth schemes are rejected with 401."""
        from acp.api.server import app, set_api_token, state

        set_api_token("secret-token-123")
        config_path = _make_repo_config(tmp_path)
        state.set_config(str(config_path))
        try:
            client = TestClient(app)

            resp = client.get("/tasks", headers={"Authorization": "Basic abc123"})
            assert resp.status_code == 401
        finally:
            set_api_token(None)


# --------------------------------------------------------------------------- #
# CORS configuration via ApiSection
# --------------------------------------------------------------------------- #


@api_skip
class TestCORSConfig:
    """Tests for CORS configuration via the ApiSection in RepoConfig."""

    def test_api_section_defaults(self):
        """ApiSection has sensible defaults (CORS enabled, dev origins)."""
        from acp.config import ApiSection

        api = ApiSection()
        assert api.cors_enabled is True
        assert api.cors_origins == []

    def test_api_section_custom_origins(self):
        """ApiSection accepts custom cors_origins."""
        from acp.config import ApiSection

        api = ApiSection(cors_origins=["https://acp.internal.corp"])
        assert api.cors_origins == ["https://acp.internal.corp"]

    def test_api_section_cors_disabled(self):
        """ApiSection can disable CORS."""
        from acp.config import ApiSection

        api = ApiSection(cors_enabled=False)
        assert api.cors_enabled is False

    def test_repo_config_includes_api_section(self, tmp_path):
        """RepoConfig loads the api section from YAML."""
        from acp.config import load_repo_config

        config_path = tmp_path / "test.repo.yaml"
        config_path.write_text(
            f"repo:\n"
            f"  name: test-repo\n"
            f"  path: {tmp_path}\n"
            f"  default_branch: main\n"
            f"agent:\n"
            f"  default: shell\n"
            f"api:\n"
            f"  cors_origins:\n"
            f"    - https://acp.internal.corp\n"
            f"    - https://ui.acp.internal.corp\n"
        )
        cfg = load_repo_config(config_path)
        assert cfg.api.cors_enabled is True
        assert cfg.api.cors_origins == [
            "https://acp.internal.corp",
            "https://ui.acp.internal.corp",
        ]

    def test_cors_env_var_origins(self, tmp_path, monkeypatch):
        """ACP_CORS_ORIGINS env var sets custom origins."""
        from acp.api.server import _resolve_cors_origins

        monkeypatch.setenv("ACP_CORS_ORIGINS", "https://a.com,https://b.com")
        origins = _resolve_cors_origins()
        assert origins == ["https://a.com", "https://b.com"]

    def test_cors_env_var_disabled(self, tmp_path, monkeypatch):
        """ACP_CORS_ENABLED=false disables CORS."""
        from acp.api.server import _cors_enabled

        monkeypatch.setenv("ACP_CORS_ENABLED", "false")
        assert _cors_enabled() is False

    def test_cors_env_var_defaults(self, tmp_path, monkeypatch):
        """Without env vars, dev defaults are used."""
        from acp.api.server import _cors_enabled, _resolve_cors_origins

        monkeypatch.delenv("ACP_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("ACP_CORS_ENABLED", raising=False)
        assert _cors_enabled() is True
        origins = _resolve_cors_origins()
        assert "http://localhost:5173" in origins


# --------------------------------------------------------------------------- #
# v0.7.4: Path traversal protection
# --------------------------------------------------------------------------- #


class TestPathTraversalProtection:
    """v0.7.4: API endpoints reject paths containing '..'."""

    def test_list_tasks_rejects_traversal(self, tmp_path):
        """GET /tasks with runs_root containing .. is rejected."""
        from fastapi import HTTPException

        from acp.api.server import _validate_path_param

        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("data/../etc", "runs_root")
        assert exc_info.value.status_code == 400
        assert "directory traversal" in exc_info.value.detail

    def test_validate_path_rejects_empty(self):
        """Empty path is rejected."""
        from fastapi import HTTPException

        from acp.api.server import _validate_path_param

        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("", "runs_root")
        assert exc_info.value.status_code == 400

    def test_validate_path_accepts_normal(self):
        """Normal relative paths are accepted."""
        from acp.api.server import _validate_path_param

        result = _validate_path_param("data/runs", "runs_root")
        assert result is not None

    def test_validate_path_rejects_nested_traversal(self):
        """Nested .. in path components is rejected."""
        from fastapi import HTTPException

        from acp.api.server import _validate_path_param

        with pytest.raises(HTTPException):
            _validate_path_param("data/runs/../../etc/passwd", "runs_root")


# --------------------------------------------------------------------------- #
# v0.7.4: Recursion depth enforcement
# --------------------------------------------------------------------------- #


class TestSubtaskRecursionDepth:
    """v0.7.4: Subtask spawning is blocked at max recursion depth."""

    def test_recursion_depth_zero_allows_spawn(self):
        """At depth 0, spawn requests are allowed."""
        from acp.subtask import parse_subtask_requests

        stdout = "ACP_SPAWN_SUBTASK: Do something\n"
        result = parse_subtask_requests(stdout, recursion_depth=0)
        assert len(result.requests) == 1

    def test_recursion_depth_max_blocks_spawn(self):
        """At max depth, spawn requests are rejected."""
        from acp.models import MAX_SUBTASK_RECURSION_DEPTH
        from acp.subtask import parse_subtask_requests

        stdout = "ACP_SPAWN_SUBTASK: Do something\n"
        result = parse_subtask_requests(stdout, recursion_depth=MAX_SUBTASK_RECURSION_DEPTH)
        assert len(result.requests) == 0

    def test_recursion_depth_above_max_blocks_spawn(self):
        """Above max depth, spawn requests are rejected."""
        from acp.models import MAX_SUBTASK_RECURSION_DEPTH
        from acp.subtask import parse_subtask_requests

        stdout = "ACP_SPAWN_SUBTASK: Do something\n"
        result = parse_subtask_requests(stdout, recursion_depth=MAX_SUBTASK_RECURSION_DEPTH + 1)
        assert len(result.requests) == 0

    def test_recursion_depth_below_max_allows_spawn(self):
        """Just below max depth, spawn requests are still allowed."""
        from acp.models import MAX_SUBTASK_RECURSION_DEPTH
        from acp.subtask import parse_subtask_requests

        stdout = "ACP_SPAWN_SUBTASK: Do something\n"
        result = parse_subtask_requests(stdout, recursion_depth=MAX_SUBTASK_RECURSION_DEPTH - 1)
        assert len(result.requests) == 1

    def test_spawn_lines_stripped_even_when_blocked(self):
        """Spawn lines are stripped from cleaned_stdout even when blocked."""
        from acp.models import MAX_SUBTASK_RECURSION_DEPTH
        from acp.subtask import parse_subtask_requests

        stdout = "line1\nACP_SPAWN_SUBTASK: Do something\nline2\n"
        result = parse_subtask_requests(stdout, recursion_depth=MAX_SUBTASK_RECURSION_DEPTH)
        assert len(result.requests) == 0
        assert "ACP_SPAWN_SUBTASK" not in result.cleaned_stdout
        assert "line1" in result.cleaned_stdout
        assert "line2" in result.cleaned_stdout


# --------------------------------------------------------------------------- #
# v0.7.4: Egress domain validation
# --------------------------------------------------------------------------- #


class TestEgressDomainValidation:
    """v0.7.4: EgressLogger rejects malformed domains."""

    def test_empty_domain_rejected(self):
        """Empty domain is not logged."""
        from acp.egress import EgressLogger

        logger = EgressLogger()
        logger.log_request("", method="GET", path="/", status_code=200)
        assert len(logger.events) == 0

    def test_whitespace_domain_rejected(self):
        """Whitespace-only domain is not logged."""
        from acp.egress import EgressLogger

        logger = EgressLogger()
        logger.log_request("   ", method="GET", path="/", status_code=200)
        assert len(logger.events) == 0

    def test_control_chars_rejected(self):
        """Domains with control characters are not logged."""
        from acp.egress import EgressLogger

        logger = EgressLogger()
        logger.log_request("evil\x00.com", method="GET", path="/", status_code=200)
        assert len(logger.events) == 0

    def test_valid_domain_accepted(self):
        """Valid domains are logged normally."""
        from acp.egress import EgressLogger

        logger = EgressLogger()
        logger.log_request("pypi.org", method="GET", path="/", status_code=200)
        assert len(logger.events) == 1
        assert logger.events[0].domain == "pypi.org"

    def test_domain_lowercased(self):
        """Domains are lowercased."""
        from acp.egress import EgressLogger

        logger = EgressLogger()
        logger.log_request("PyPI.Org", method="GET", path="/", status_code=200)
        assert logger.events[0].domain == "pypi.org"


# --------------------------------------------------------------------------- #
# v0.7.4: DurableTaskStore status validation
# --------------------------------------------------------------------------- #


class TestDurableTaskStoreValidation:
    """v0.7.4: DurableTaskStore validates status and repo_name parameters."""

    def test_invalid_status_rejected(self, tmp_path):
        """Query with invalid status raises ValueError."""
        from acp.evidence.durable_task_store import DurableTaskStore

        store = DurableTaskStore(tmp_path / "test.db")
        store.init()
        with pytest.raises(ValueError, match="Invalid status"):
            store.query(status="DROP TABLE tasks;--")
        store.close()

    def test_repo_name_with_sql_chars_rejected(self, tmp_path):
        """Query with SQL metacharacters in repo_name raises ValueError."""
        from acp.evidence.durable_task_store import DurableTaskStore

        store = DurableTaskStore(tmp_path / "test.db")
        store.init()
        with pytest.raises(ValueError, match="SQL metacharacters"):
            store.query(repo_name="test'; DROP TABLE tasks;--")
        store.close()

    def test_valid_status_accepted(self, tmp_path):
        """Query with valid TaskStatus works."""
        from acp.evidence.durable_task_store import DurableTaskStore
        from acp.models import TaskStatus

        store = DurableTaskStore(tmp_path / "test.db")
        store.init()
        # Should not raise.
        results = store.query(status=TaskStatus.CREATED)
        assert results == []
        store.close()
