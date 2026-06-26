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
from pathlib import Path
from typing import Any

from acp.config import load_repo_config
from acp.errors import ACPError
from acp.models import EventType, TaskStatus
from acp.store import TaskStore

# FastAPI is an optional dependency (the `api` extra).
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:
    raise ImportError(
        "FastAPI is not installed. Install with: uv sync --extra api"
    ) from None


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


class MemorySearchRequest(BaseModel):
    """Request body for GET /memory/search."""
    query: str
    num_results: int = 10


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
                    status_code=500,
                    detail="No config path set. Use --config or POST /config."
                )
            try:
                self._config_cache = load_repo_config(Path(self.config_path))
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"Config file not found: {exc}"
                ) from exc
        return self._config_cache


state = ServerState()
app = FastAPI(
    title="Agent Control Plane",
    description="Local HTTP API for the ACP workflow",
    version="0.6.6",
)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check."""
    return {"status": "ok", "version": "0.6.6"}


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

    from acp.graph.workflow import run_workflow

    try:
        result = run_workflow(
            config=cfg,
            user_request=request.task,
            runs_root=Path(request.runs_root),
            vault_root=Path(request.vault_root),
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
        vault_note_path=str(result.get("vault_note_path")) if result.get("vault_note_path") else None,
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

    # Run in a background thread (the workflow is synchronous).
    async def _run() -> None:
        from acp.graph.workflow import run_workflow
        try:
            run_workflow(
                config=cfg,
                user_request=request.task,
                runs_root=Path(request.runs_root),
                vault_root=Path(request.vault_root),
            )
        except Exception:  # noqa: BLE001
            pass  # Error is recorded in the event log

    asyncio.create_task(_run())
    return {"status": "started", "task": request.task}


@app.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(runs_root: str = "data/runs") -> list[TaskSummary]:
    """List all tasks."""
    store = TaskStore(runs_root=Path(runs_root))
    tasks: list[TaskSummary] = []

    if not store.root.is_dir():
        return tasks

    for task_dir in sorted(store.root.iterdir()):
        task_json = task_dir / "task.json"
        if not task_json.is_file():
            continue
        try:
            task = store.load(task_dir.name)
            tasks.append(TaskSummary(
                task_id=task.task_id,
                repo_name=task.repo_name,
                status=task.status.value,
                user_request=task.user_request,
                created_at=task.created_at,
                updated_at=task.updated_at,
            ))
        except Exception:  # noqa: BLE001
            continue

    return tasks


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    runs_root: str = "data/runs",
) -> dict[str, Any]:
    """Get task status and metadata."""
    store = TaskStore(runs_root=Path(runs_root))
    try:
        task = store.load(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Task not found: {exc}") from exc

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
    """Approve a task's vault note."""
    from acp.events import EventWriter
    from acp.vault.approval import approve_vault_note, can_approve

    store = TaskStore(runs_root=Path(runs_root))
    try:
        task = store.load(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Task not found: {exc}") from exc

    if not can_approve(task.status):
        raise HTTPException(
            status_code=400,
            detail=f"Task status '{task.status.value}' — only 'passed' or 'needs_review' can be approved."
        )

    note_path = Path(vault_root) / "tasks" / f"{task_id}.md"
    if not note_path.is_file():
        raise HTTPException(status_code=404, detail=f"Vault note not found: {note_path}")

    try:
        approve_vault_note(note_path, approver=request.approver)
        # Write the event
        run_dir = store.run_dir(task_id)
        if run_dir.is_dir():
            events = EventWriter(task_id, run_dir)
            events.write(EventType.HUMAN_APPROVED, {"approver": request.approver})
        task.status = TaskStatus.APPROVED
        task.touch()
        store.save(task)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "approved", "task_id": task_id}


@app.post("/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    request: RejectRequest,
    runs_root: str = "data/runs",
    vault_root: str = "vault",
) -> dict[str, Any]:
    """Reject a task's vault note."""
    from acp.events import EventWriter
    from acp.vault.approval import reject_vault_note

    store = TaskStore(runs_root=Path(runs_root))
    try:
        task = store.load(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Task not found: {exc}") from exc

    note_path = Path(vault_root) / "tasks" / f"{task_id}.md"
    if not note_path.is_file():
        raise HTTPException(status_code=404, detail=f"Vault note not found: {note_path}")

    try:
        reject_vault_note(note_path, rejecter=request.rejecter)
        run_dir = store.run_dir(task_id)
        if run_dir.is_dir():
            events = EventWriter(task_id, run_dir)
            events.write(EventType.HUMAN_REJECTED, {"rejecter": request.rejecter})
        task.status = TaskStatus.REJECTED
        task.touch()
        store.save(task)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "rejected", "task_id": task_id}


@app.get("/tasks/{task_id}/events", response_model=list[EventResponse])
async def get_events(
    task_id: str,
    runs_root: str = "data/runs",
) -> list[EventResponse]:
    """Get the event log for a task."""
    from acp.events import EventWriter

    store = TaskStore(runs_root=Path(runs_root))
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
    store = TaskStore(runs_root=Path(runs_root))
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
            detail=f"Memory extra not installed: {exc}. Install with: uv sync --extra memory"
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
        raise HTTPException(
            status_code=404,
            detail="UI not built. Run: cd ui && npm run build"
        )
