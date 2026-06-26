"""ACP FastAPI control layer (M10).

Exposes the ACP workflow as a local HTTP API so the CLI workflow is
controllable via endpoints. This is the bridge between the terminal-
first CLI and the future React UI (M11).

Endpoints:
  POST /tasks/run          — run a coding task (sync)
  POST /tasks/run/async    — run a coding task (background)
  GET  /tasks              — list all tasks
  GET  /tasks/{task_id}    — get task status
  POST /tasks/{task_id}/approve — approve a task's vault note
  POST /tasks/{task_id}/reject  — reject a task's vault note
  GET  /tasks/{task_id}/events  — get event log
  GET  /tasks/{task_id}/report  — get report content
  GET  /health             — health check
  GET  /memory/search      — search temporal memory
  POST /memory/promote     — promote approved notes to Graphiti

Install:
  uv sync --extra api

Start:
  acp serve --config configs/repos/example.repo.yaml
  # or
  uvicorn acp.api.server:app --reload
"""

from acp.api.server import app

__all__ = ["app"]
