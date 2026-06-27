"""FastAPI server — local HTTP API over the ACP workflow.

Exposes the same operations as the CLI (run, approve, reject, list,
events, verify, memory) as HTTP endpoints. The server holds a single
repo config and runs all tasks against it.

Usage::

    uv sync --extra api
    uvicorn acp.api.server:app --reload
    # or
    acp serve --config configs/repos/example.repo.yaml
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from acp.config import load_repo_config
from acp.errors import ACPError
from acp.models import EventType, TaskStatus
from acp.store import TaskStore, is_valid_task_id

# FastAPI is an optional dependency (the `api` extra).
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:
    raise ImportError("FastAPI is not installed. Install with: uv sync --extra api") from None

import logging

_logger = logging.getLogger("acp.api")

# --------------------------------------------------------------------------- #
# Bearer token auth middleware
# --------------------------------------------------------------------------- #

# Endpoints that don't require authentication (always public).
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})

# The expected bearer token. When None, auth is disabled (backwards-
# compatible for local development). Set via ``ACP_API_TOKEN`` env var
# or the ``--api-token`` CLI flag. Uses ``secrets.compare_digest`` to
# prevent timing attacks on token comparison.
_api_token: str | None = None


def set_api_token(token: str | None) -> None:
    """Set the expected bearer token for the API.

    When set to a non-empty string, all non-public endpoints require an
    ``Authorization: Bearer <token>`` header. When None or empty, auth
    is disabled (for local development only).
    """
    global _api_token
    _api_token = token if token else None


def get_api_token() -> str | None:
    """Return the configured API token, or None if auth is disabled."""
    return _api_token


def _init_token_from_env() -> None:
    """Initialize the API token from the ``ACP_API_TOKEN`` env var if set."""
    env_token = os.environ.get("ACP_API_TOKEN", "").strip()
    if env_token:
        set_api_token(env_token)


# Initialize from env var on module import (so the server picks it up
# even when started via uvicorn directly rather than the CLI).
_init_token_from_env()


try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class BearerTokenMiddleware(BaseHTTPMiddleware):
        """Validate ``Authorization: Bearer <token>`` on non-public routes.

        When ``_api_token`` is set, every request to a non-public path
        must include a valid bearer token. Returns 401 if the header is
        missing or malformed, 403 if the token doesn't match.
        """

        async def dispatch(self, request: Request, call_next: Any) -> Any:
            # Public paths are always accessible.
            if request.url.path in _PUBLIC_PATHS:
                return await call_next(request)

            # If no token is configured, auth is disabled (local dev).
            expected = get_api_token()
            if expected is None:
                return await call_next(request)

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Missing or invalid Authorization header. "
                        "Expected: Bearer <token>"
                    },
                )

            provided = auth_header[7:]  # strip "Bearer "
            if not secrets.compare_digest(provided, expected):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid API token."},
                )

            return await call_next(request)

except ImportError:
    # Starlette is a FastAPI dependency — if FastAPI isn't installed,
    # the import at the top already raised. This guard is defensive.
    BearerTokenMiddleware = None  # type: ignore[assignment, misc]


def _recover_orphaned_tasks() -> None:
    """Recover tasks orphaned by a previous server crash.

    Scans for tasks in non-terminal states (created, executing, reviewing)
    and marks them as FAILED. Also cleans up git worktrees.
    """
    runs_root = state.runs_root
    durable_db_path = runs_root / "tasks.db"

    # Try the DurableTaskStore first (if it has data).
    if durable_db_path.is_file():
        try:
            from acp.evidence.durable_task_store import DurableTaskStore

            with DurableTaskStore(durable_db_path) as db:
                recovered = db.recover_orphaned_tasks(runs_root=runs_root)
                for tid in recovered:
                    _logger.info("Recovered orphaned task: %s", tid)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("DurableTaskStore recovery failed: %s", exc)

    # Also scan task.json files directly (covers no-DurableTaskStore case).
    try:
        store = TaskStore(runs_root=runs_root)
        if store.root.is_dir():
            for task_dir in sorted(store.root.iterdir()):
                task_json = task_dir / "task.json"
                if not task_json.is_file():
                    continue
                try:
                    task = store.load(task_dir.name)
                    if task.status in (
                        TaskStatus.CREATED,
                        TaskStatus.EXECUTING,
                        TaskStatus.REVIEWING,
                    ):
                        task.status = TaskStatus.FAILED
                        task.touch()
                        store.save(task)
                        _logger.info("Recovered orphaned task from task.json: %s", task.task_id)
                except Exception as exc:  # noqa: BLE001
                    # v0.7.4: Log the error instead of silently passing.
                    # A corrupt task.json could indicate evidence tampering
                    # or a filesystem issue — the operator needs to know.
                    _logger.error(
                        "Failed to load task.json during orphan recovery (path=%s): %s",
                        task_json,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("task.json scan for orphans failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Startup: recover orphaned tasks. Shutdown: nothing to clean up."""
    _recover_orphaned_tasks()
    yield


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class RunRequest(BaseModel):
    """Request body for POST /tasks/run."""

    task: str
    config_path: str = ""
    vault_root: str = "vault"
    runs_root: str = "data/runs"


class RunResponse(BaseModel):
    """Response for POST /tasks/run."""

    task_id: str
    status: str
    report_path: str | None = None
    vault_note_path: str | None = None
    error: str | None = None


class ApproveRequest(BaseModel):
    """Request body for POST /tasks/{task_id}/approve."""

    approver: str = ""


class RejectRequest(BaseModel):
    """Request body for POST /tasks/{task_id}/reject."""

    rejecter: str = ""
    reason: str = ""


class TaskSummary(BaseModel):
    """Summary of a task for the list endpoint."""

    task_id: str
    repo_name: str
    status: str
    user_request: str
    created_at: str
    updated_at: str


class EventResponse(BaseModel):
    """A single event from the event log."""

    event_id: str
    task_id: str
    type: str
    timestamp: str
    payload: dict[str, Any] = {}
    prev_hash: str = ""
    hash: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _validate_task_id(task_id: str) -> None:
    """Reject non-canonical task IDs to prevent path traversal."""
    if not is_valid_task_id(task_id):
        raise HTTPException(
            status_code=400,
            detail=(f"Invalid task id: {task_id!r} (expected task_<YYYYMMDD>_<NNNN>)"),
        )


def _load_task_or_404(
    task_id: str,
    runs_root: str,
) -> tuple[Any, TaskStore]:
    """Validate task_id, load the task, and return (task, store).

    Raises HTTPException(400) for invalid IDs and 404 for missing tasks.
    """
    _validate_task_id(task_id)
    safe_runs_root = _validate_path_param(runs_root, "runs_root")
    store = TaskStore(runs_root=safe_runs_root)
    try:
        task = store.load(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=404,
            detail=f"Task not found: {exc}",
        ) from exc
    return task, store


def _lifecycle_action(
    *,
    task_id: str,
    runs_root: str,
    vault_root: str,
    event_type: EventType,
    new_status: TaskStatus,
    actor: str,
    reason: str = "",
    status_check: Any,
    status_error: str,
) -> dict[str, Any]:
    """Shared logic for approve and reject endpoints."""
    from acp.evidence.lifecycle import (
        record_lifecycle_event,
        rerender_vault_note_from_state,
    )

    task, store = _load_task_or_404(task_id, runs_root)

    if not status_check(task.status):
        raise HTTPException(status_code=400, detail=status_error)

    safe_vault_root = _validate_path_param(vault_root, "vault_root")
    note_path = safe_vault_root / "tasks" / f"{task_id}.md"
    if not note_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Vault note not found: {note_path}",
        )

    run_dir = store.run_dir(task_id)
    if not run_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Run directory not found: {run_dir}",
        )

    payload: dict[str, Any] = {
        "actor": actor,
        "vault_note_path": str(note_path),
    }
    if reason:
        payload["reason"] = reason

    try:
        durable_warning = record_lifecycle_event(
            task_id=task_id,
            run_dir=run_dir,
            event_type=event_type,
            payload=payload,
        )
    except ACPError as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Lifecycle event write failed: {exc}",
        ) from exc

    task.status = new_status
    task.touch()
    store.save(task)

    rerender_vault_note_from_state(
        note_path=note_path,
        run_dir=run_dir,
        task=task,
        store=store,
        vault_root=safe_vault_root,
        on_warning=lambda msg: _logger.warning(msg),
    )

    action = "approved" if new_status == TaskStatus.APPROVED else "rejected"
    result: dict[str, Any] = {"status": action, "task_id": task_id}
    if durable_warning:
        result["warning"] = durable_warning
    return result


# --------------------------------------------------------------------------- #
# Server state
# --------------------------------------------------------------------------- #


class ServerState:
    """Holds the server's configuration and shared state."""

    def __init__(self) -> None:
        self.config_path: str = ""
        self.vault_root: Path = Path("vault")
        self.runs_root: Path = Path("data/runs")
        self._config_cache: Any = None

    def set_config(self, config_path: str) -> None:
        """Set and load the repo config."""
        self.config_path = config_path
        self._config_cache = None  # force reload

    def get_config(self) -> Any:
        """Load and cache the repo config."""
        if self._config_cache is None:
            if not self.config_path:
                raise HTTPException(
                    status_code=500, detail="No config path set. Use --config or POST /config."
                )
            try:
                self._config_cache = load_repo_config(Path(self.config_path))
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=404, detail=f"Config file not found: {exc}"
                ) from exc
        return self._config_cache


state = ServerState()
app = FastAPI(
    title="Agent Control Plane",
    description="Local HTTP API for the ACP workflow",
    version="0.6.7",
    lifespan=lifespan,
)

# Default CORS origins for local development (Vite dev server on :5173,
# and localhost:3000 for alternative dev setups). In production, operators
# should set ``api.cors_origins`` in the repo config — the CLI ``serve``
# command reads it and sets the ``ACP_CORS_ORIGINS`` env var before
# starting uvicorn, so the server picks it up at import time. Set
# ``ACP_CORS_ENABLED=false`` to disable CORS entirely (same-origin deploys).
_DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _resolve_cors_origins() -> list[str]:
    """Resolve CORS origins from env var or dev defaults.

    The ``ACP_CORS_ORIGINS`` env var (comma-separated) takes precedence.
    The CLI ``serve`` command sets this from ``api.cors_origins`` in the
    repo config before starting uvicorn.
    """
    env_origins = os.environ.get("ACP_CORS_ORIGINS", "").strip()
    if env_origins:
        return [o.strip() for o in env_origins.split(",") if o.strip()]
    return _DEV_CORS_ORIGINS


def _cors_enabled() -> bool:
    """Whether CORS middleware should be added."""
    return os.environ.get("ACP_CORS_ENABLED", "true").strip().lower() != "false"


# CORS middleware — origins resolved from env var at import time.
# v0.7.4: Restrict methods and headers to only what the API needs,
# instead of the overly permissive ["*"].
if _cors_enabled():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Accept"],
    )


# Bearer token auth — added after CORS so CORS preflight (OPTIONS) is
# handled before the auth check. When no token is configured, the
# middleware is a pass-through.
if BearerTokenMiddleware is not None:
    app.add_middleware(BearerTokenMiddleware)


# --------------------------------------------------------------------------- #
# Rate limiting (v0.7.4)
# --------------------------------------------------------------------------- #


class RateLimitMiddleware:
    """Simple in-memory rate limiter using a sliding window.

    v0.7.4: Lightweight rate limiting without external dependencies.
    Tracks request timestamps per client IP in memory. When the number
    of requests in the window exceeds the limit, returns 429 Too Many
    Requests.

    This is defense-in-depth on top of the bearer token auth — it
    prevents authenticated clients from overwhelming the API with
    expensive operations like /tasks/run.

    Configurable via environment variables:
      - ACP_RATE_LIMIT_ENABLED: "false" to disable (default: enabled)
      - ACP_RATE_LIMIT_REQUESTS: max requests per window (default: 60)
      - ACP_RATE_LIMIT_WINDOW: window size in seconds (default: 60)
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.enabled = os.environ.get("ACP_RATE_LIMIT_ENABLED", "true").strip().lower() != "false"
        self.max_requests = int(os.environ.get("ACP_RATE_LIMIT_REQUESTS", "60"))
        self.window_seconds = int(os.environ.get("ACP_RATE_LIMIT_WINDOW", "60"))
        # client_ip -> list of timestamps
        self._requests: dict[str, list[float]] = {}
        self._cleanup_counter = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not self.enabled or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Extract client IP.
        client_ip = scope.get("client", ["unknown", None])[0] if scope.get("client") else "unknown"
        path = scope.get("path", "")

        # Skip rate limiting for health checks and docs.
        if path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        import time

        now = time.monotonic()
        window_start = now - self.window_seconds

        # Get or create request list for this client.
        if client_ip not in self._requests:
            self._requests[client_ip] = []
        timestamps = self._requests[client_ip]

        # Remove expired timestamps.
        while timestamps and timestamps[0] < window_start:
            timestamps.pop(0)

        # Check limit.
        if len(timestamps) >= self.max_requests:
            # Send 429 response.
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(self.window_seconds).encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(
                        {"detail": "Rate limit exceeded. Try again in a few seconds."}
                    ).encode(),
                }
            )
            return

        # Record this request.
        timestamps.append(now)

        # Periodic cleanup of stale entries to prevent memory growth.
        self._cleanup_counter += 1
        if self._cleanup_counter > 1000:
            self._cleanup_counter = 0
            stale_ips = [
                ip
                for ip, ts_list in self._requests.items()
                if not ts_list or ts_list[-1] < window_start
            ]
            for ip in stale_ips:
                del self._requests[ip]

        await self.app(scope, receive, send)


app.add_middleware(RateLimitMiddleware)


# --------------------------------------------------------------------------- #
# Path validation
# --------------------------------------------------------------------------- #


def _validate_path_param(path_str: str, param_name: str) -> Path:
    """Validate a user-supplied path parameter for safety.

    v0.7.4: Reject paths containing ``..`` (directory traversal) or
    absolute paths that escape the expected data/vault directories.
    This prevents an attacker from using the API to read arbitrary
    filesystem locations via ``runs_root=/etc`` or similar.

    Returns the resolved Path object if safe, raises HTTPException(400) otherwise.
    """
    from fastapi import HTTPException

    if not path_str or not path_str.strip():
        raise HTTPException(status_code=400, detail=f"{param_name} must not be empty")

    # Reject paths with directory traversal components.
    parts = Path(path_str).parts
    if ".." in parts:
        raise HTTPException(
            status_code=400,
            detail=f"{param_name} must not contain '..' (directory traversal denied)",
        )

    resolved = Path(path_str).resolve()
    return resolved


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check."""
    return {"status": "ok", "version": "0.6.7"}


@app.post("/config")
async def set_config(config_path: str = Query(...)) -> dict[str, str]:
    """Set the repo config path for subsequent operations."""
    state.set_config(config_path)
    # Validate it loads
    state.get_config()
    return {"status": "ok", "config_path": config_path}


@app.post("/tasks/run", response_model=RunResponse)
async def run_task(request: RunRequest) -> RunResponse:
    """Run a coding task synchronously.

    This blocks until the task completes (may take minutes for real
    agents). For long-running tasks, use POST /tasks/run/async instead.
    """
    cfg = state.get_config()
    if request.config_path:
        state.set_config(request.config_path)
        cfg = state.get_config()

    safe_runs_root = _validate_path_param(request.runs_root, "runs_root")
    safe_vault_root = _validate_path_param(request.vault_root, "vault_root")

    from acp.graph.workflow import run_workflow

    try:
        result = await asyncio.to_thread(
            run_workflow,
            config=cfg,
            user_request=request.task,
            runs_root=safe_runs_root,
            vault_root=safe_vault_root,
        )
    except ACPError as exc:
        raise HTTPException(status_code=exc.exit_code, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    status = result.get("status")
    if isinstance(status, TaskStatus):
        status_str = status.value
    else:
        status_str = str(status or "unknown")

    return RunResponse(
        task_id=result.get("task_id", ""),
        status=status_str,
        report_path=str(result.get("report_path")) if result.get("report_path") else None,
        vault_note_path=str(result.get("vault_note_path"))
        if result.get("vault_note_path")
        else None,
        error=result.get("error"),
    )


@app.post("/tasks/run/async")
async def run_task_async(request: RunRequest) -> dict[str, str]:
    """Start a coding task in the background.

    Returns immediately with the task_id. Poll GET /tasks/{task_id}
    for status.
    """
    cfg = state.get_config()
    if request.config_path:
        state.set_config(request.config_path)
        cfg = state.get_config()

    safe_runs_root = _validate_path_param(request.runs_root, "runs_root")
    safe_vault_root = _validate_path_param(request.vault_root, "vault_root")

    # Pre-generate the task_id so the client can poll immediately.
    store = TaskStore(runs_root=safe_runs_root)
    task_id = store.next_task_id(repo_path=cfg.repo.path)

    # Run in a background thread so the blocking workflow doesn't
    # stall the event loop.
    async def _run() -> None:
        from acp.graph.workflow import run_workflow

        try:
            await asyncio.to_thread(
                run_workflow,
                config=cfg,
                user_request=request.task,
                runs_root=safe_runs_root,
                vault_root=safe_vault_root,
                task_id=task_id,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error("async task %s failed: %s", task_id, exc)

    asyncio.create_task(_run())
    return {"status": "started", "task_id": task_id, "task": request.task}


@app.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(
    runs_root: str = "data/runs",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[TaskSummary]:
    """List tasks with pagination.

    Returns up to ``limit`` tasks starting from ``offset`` (newest first
    by directory name, which sorts chronologically). Use ``limit=0`` for
    all tasks (backwards-compatible behavior).
    """
    safe_runs_root = _validate_path_param(runs_root, "runs_root")
    store = TaskStore(runs_root=safe_runs_root)
    tasks: list[TaskSummary] = []

    if not store.root.is_dir():
        return tasks

    # Collect valid task dir names sorted newest-first.
    task_dirs = sorted(
        (d for d in store.root.iterdir() if d.is_dir() and (d / "task.json").is_file()),
        reverse=True,
    )

    if limit > 0:
        task_dirs = task_dirs[offset : offset + limit]
    elif offset > 0:
        task_dirs = task_dirs[offset:]

    for task_dir in task_dirs:
        try:
            task = store.load(task_dir.name)
            tasks.append(
                TaskSummary(
                    task_id=task.task_id,
                    repo_name=task.repo_name,
                    status=task.status.value,
                    user_request=task.user_request,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # v0.7.4: Log the error instead of silently continuing.
            _logger.error("Failed to load task from %s: %s", task_dir, exc)
            continue

    return tasks


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    runs_root: str = "data/runs",
) -> dict[str, Any]:
    """Get task status and metadata."""
    task, _store = _load_task_or_404(task_id, runs_root)

    return {
        "task_id": task.task_id,
        "repo_name": task.repo_name,
        "status": task.status.value,
        "user_request": task.user_request,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "base_branch": task.base_branch,
        "task_branch": task.task_branch,
    }


@app.post("/tasks/{task_id}/approve")
async def approve_task(
    task_id: str,
    request: ApproveRequest,
    runs_root: str = "data/runs",
    vault_root: str = "vault",
) -> dict[str, Any]:
    """Approve a task's vault note.

    Uses the shared lifecycle service (acp.evidence.lifecycle) to ensure
    full transactional integrity: signed events, SQLite dual-writes,
    manifest recompute, and rollback on failure — same as ``acp approve``.
    """
    from acp.vault.approval import can_approve

    return _lifecycle_action(
        task_id=task_id,
        runs_root=runs_root,
        vault_root=vault_root,
        event_type=EventType.HUMAN_APPROVED,
        new_status=TaskStatus.APPROVED,
        actor=request.approver or "unknown",
        status_check=can_approve,
        status_error=(
            "Task status is not approvable — only 'passed' or 'needs_review' can be approved."
        ),
    )


@app.post("/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    request: RejectRequest,
    runs_root: str = "data/runs",
    vault_root: str = "vault",
) -> dict[str, Any]:
    """Reject a task's vault note.

    Uses the shared lifecycle service (acp.evidence.lifecycle) to ensure
    full transactional integrity — same as ``acp reject``.
    """
    _non_rejectable = frozenset(
        {
            TaskStatus.APPROVED,
            TaskStatus.REJECTED,
            TaskStatus.ARCHIVED,
            TaskStatus.CREATED,
            TaskStatus.EXECUTING,
            TaskStatus.REVIEWING,
            TaskStatus.REPAIRING,
            TaskStatus.TESTING,
            TaskStatus.WORKTREE_CREATED,
            TaskStatus.CONTEXT_BUILT,
        }
    )

    def _can_reject(status: TaskStatus) -> bool:
        return status not in _non_rejectable

    return _lifecycle_action(
        task_id=task_id,
        runs_root=runs_root,
        vault_root=vault_root,
        event_type=EventType.HUMAN_REJECTED,
        new_status=TaskStatus.REJECTED,
        actor=request.rejecter or "unknown",
        reason=request.reason,
        status_check=_can_reject,
        status_error=(
            "Task status cannot be rejected — only PASSED, FAILED,"
            " or NEEDS_REVIEW tasks can be rejected."
        ),
    )


@app.get("/tasks/{task_id}/events", response_model=list[EventResponse])
async def get_events(
    task_id: str,
    runs_root: str = "data/runs",
) -> list[EventResponse]:
    """Get the event log for a task."""
    _validate_task_id(task_id)
    from acp.events import EventWriter

    safe_runs_root = _validate_path_param(runs_root, "runs_root")
    store = TaskStore(runs_root=safe_runs_root)
    run_dir = store.run_dir(task_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run directory not found: {run_dir}")

    writer = EventWriter(task_id, run_dir)
    events = writer.read_all()

    return [
        EventResponse(
            event_id=e.event_id,
            task_id=e.task_id,
            type=e.type.value,
            timestamp=e.timestamp,
            payload=e.payload,
            prev_hash=e.prev_hash,
            hash=e.hash,
        )
        for e in events
    ]


@app.get("/tasks/{task_id}/report")
async def get_report(
    task_id: str,
    runs_root: str = "data/runs",
) -> dict[str, str]:
    """Get the report content for a task."""
    _validate_task_id(task_id)
    safe_runs_root = _validate_path_param(runs_root, "runs_root")
    store = TaskStore(runs_root=safe_runs_root)
    run_dir = store.run_dir(task_id)
    report_path = run_dir / "artifacts" / "report.md"
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail=f"Report not found: {report_path}")

    return {"task_id": task_id, "report": report_path.read_text(encoding="utf-8")}


@app.get("/memory/search")
async def memory_search(
    query: str = Query(...),
    num_results: int = Query(10),
) -> list[dict[str, Any]]:
    """Search Graphiti temporal memory for facts."""
    try:
        from acp.memory.graphiti_client import search_graphiti_facts
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Memory extra not installed: {exc}. Install with: uv sync --extra memory",
        ) from exc

    cfg = state.get_config()
    try:
        results = search_graphiti_facts(
            query,
            group_id=cfg.memory.graphiti_group_id,
            num_results=num_results,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return results


# --------------------------------------------------------------------------- #
# v0.7.1: Missions API (M14 — mission epics)
# --------------------------------------------------------------------------- #


class MissionSummary(BaseModel):
    mission_id: str
    goal: str
    status: str
    repo_name: str
    steps_total: int
    steps_completed: int
    created_at: str


class MissionDetailResponse(BaseModel):
    mission_id: str
    goal: str
    description: str
    repo_name: str
    status: str
    steps: list[dict[str, Any]]
    created_at: str
    updated_at: str
    completed_at: str


@app.get("/missions", response_model=list[MissionSummary])
async def list_missions(
    missions_root: str = "data/missions",
) -> list[MissionSummary]:
    """List all missions ordered by creation date."""
    from acp.missions.store import MissionStore

    store = MissionStore(missions_dir=Path(missions_root))
    missions = store.list_missions()
    return [
        MissionSummary(
            mission_id=m.mission_id,
            goal=m.goal,
            status=m.status.value,
            repo_name=m.repo_name,
            steps_total=len(m.steps),
            steps_completed=sum(1 for s in m.steps if s.status == "completed"),
            created_at=m.created_at,
        )
        for m in missions
    ]


@app.get("/missions/{mission_id}", response_model=MissionDetailResponse)
async def get_mission(
    mission_id: str,
    missions_root: str = "data/missions",
) -> MissionDetailResponse:
    """Get detailed information about a specific mission."""
    from acp.missions.store import MissionStore

    if not mission_id.startswith("mission_"):
        raise HTTPException(status_code=400, detail="Invalid mission ID format")
    store = MissionStore(missions_dir=Path(missions_root))
    mission = store.load(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return MissionDetailResponse(
        mission_id=mission.mission_id,
        goal=mission.goal,
        description=mission.description,
        repo_name=mission.repo_name,
        status=mission.status.value,
        steps=[s.model_dump() for s in mission.steps],
        created_at=mission.created_at,
        updated_at=mission.updated_at,
        completed_at=mission.completed_at,
    )


# --------------------------------------------------------------------------- #
# v0.7.1: Skills API (M8 — skills governance)
# --------------------------------------------------------------------------- #


@app.get("/skills")
async def list_skills(
    skills_dir: str = "skills",
) -> list[dict[str, Any]]:
    """List all available skill playbooks from the skills directory."""
    from acp.skills.loader import load_skills

    skills_path = Path(skills_dir)
    if not skills_path.is_dir():
        return []
    try:
        skills = load_skills(skills_path)
        return [
            {
                "name": s.get("name", ""),
                "purpose": s.get("purpose", ""),
                "rules": s.get("rules", []),
                "has_hard_blocks": bool(s.get("review_gates", {}).get("hard_blocks")),
                "has_risk_elevators": bool(s.get("review_gates", {}).get("risk_elevators")),
            }
            for s in skills.values()
        ]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load skills: {exc}")


# --------------------------------------------------------------------------- #
# SSE streaming — real-time task event updates
# --------------------------------------------------------------------------- #


@app.get("/tasks/stream")
async def stream_tasks(
    runs_root: str = "data/runs",
    poll_interval: float = Query(2.0, ge=0.5, le=30.0),
) -> Any:
    """Server-Sent Events stream of task status changes.

    Polls the runs directory every ``poll_interval`` seconds and streams
    SSE events whenever a task is created or its status changes. The UI
    consumes this via ``EventSource`` to get instant updates without polling.

    Event format::

        data: {"task_id": "task_...", "status": "executing", "repo_name": "...", ...}

    A ``heartbeat`` event is sent every 30 seconds to keep the connection alive.
    """
    safe_runs_root = _validate_path_param(runs_root, "runs_root")
    store = TaskStore(runs_root=safe_runs_root)

    async def event_stream() -> Any:
        last_statuses: dict[str, str] = {}
        last_mtimes: dict[str, float] = {}
        heartbeat_counter = 0

        while True:
            # Scan all tasks, but skip task.json files whose mtime
            # hasn't changed since the last poll.
            current: dict[str, dict[str, Any]] = {}
            if store.root.is_dir():
                for task_dir in sorted(store.root.iterdir()):
                    task_json = task_dir / "task.json"
                    if not task_json.is_file():
                        continue
                    tid = task_dir.name
                    try:
                        mtime = task_json.stat().st_mtime
                    except OSError:
                        continue
                    if tid in last_mtimes and mtime == last_mtimes[tid]:
                        # File unchanged — reuse cached status.
                        if tid in last_statuses:
                            current[tid] = {
                                "task_id": tid,
                                "status": last_statuses[tid],
                            }
                        continue
                    last_mtimes[tid] = mtime
                    try:
                        task = store.load(tid)
                        current[task.task_id] = {
                            "task_id": task.task_id,
                            "status": task.status.value,
                            "repo_name": task.repo_name,
                            "user_request": task.user_request,
                        }
                    except Exception as exc:  # noqa: BLE001
                        # v0.7.4: Log the error instead of silently continuing.
                        _logger.error("SSE: Failed to load task %s: %s", tid, exc)
                        continue

            # Emit events for new or changed tasks.
            for tid, info in current.items():
                prev_status = last_statuses.get(tid)
                if prev_status != info["status"]:
                    yield f"data: {json.dumps(info)}\n\n"
                    last_statuses[tid] = info["status"]

            # Emit events for tasks that disappeared (cleanup).
            for tid in list(last_statuses.keys()):
                if tid not in current:
                    yield f"data: {json.dumps({'task_id': tid, 'status': 'removed'})}\n\n"
                    del last_statuses[tid]

            # Heartbeat every ~30 seconds.
            heartbeat_counter += 1
            if heartbeat_counter >= int(30 / poll_interval):
                yield ": heartbeat\n\n"
                heartbeat_counter = 0

            await asyncio.sleep(poll_interval)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# v0.6.6 (M11): Serve the React UI (built from ui/dist/)
# --------------------------------------------------------------------------- #

# The React dashboard is built with `npm run build` in the ui/ directory.
# The output goes to ui/dist/. We mount it as static files at /ui/ and
# serve index.html at /ui (the root dashboard).
_UI_DIST = Path(__file__).resolve().parent.parent.parent.parent / "ui" / "dist"

if _UI_DIST.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        """Serve the React dashboard (built from ui/dist/)."""
        index = _UI_DIST / "index.html"
        if index.is_file():
            return index.read_text(encoding="utf-8")
        raise HTTPException(status_code=404, detail="UI not built. Run: cd ui && npm run build")
