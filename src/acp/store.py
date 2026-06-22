"""Task store — run-directory and task.json persistence.

Owns the layout under ``data/runs/`` and the monotonic task-id generator.
A task id is ``task_<YYYYMMDD>_<NNNN>`` where the sequence restarts each
UTC day, so ids sort chronologically and are human-readable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from acp.models import Task, TaskStatus

# task_20260621_0001
_TASK_ID_RE = re.compile(r"^task_(\d{8})_(\d{4})$")


class TaskStore:
    """Creates and reads task run directories and task.json files.

    Args:
        runs_root: Directory holding one subdir per task. Defaults to
            ``data/runs`` relative to cwd.
    """

    def __init__(self, runs_root: str | Path | None = None) -> None:
        self.root = Path(runs_root or "data/runs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # IDs
    # ------------------------------------------------------------------ #

    def next_task_id(self, *, now: datetime | None = None) -> str:
        """Return the next task id for today, scanning existing run dirs."""
        today = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
        prefix = f"task_{today}_"
        seq = 0
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith(prefix):
                m = _TASK_ID_RE.match(child.name)
                if m and int(m.group(2)) > seq:
                    seq = int(m.group(2))
        return f"{prefix}{seq + 1:04d}"

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def run_dir(self, task_id: str) -> Path:
        return self.root / task_id

    def artifacts_dir(self, task_id: str) -> Path:
        return self.run_dir(task_id) / "artifacts"

    def worktree_path(self, task_id: str) -> Path:
        return self.run_dir(task_id) / "worktree"

    def task_json_path(self, task_id: str) -> Path:
        return self.run_dir(task_id) / "task.json"

    def events_path(self, task_id: str) -> Path:
        return self.run_dir(task_id) / "events.jsonl"

    # ------------------------------------------------------------------ #
    # Create / read / update
    # ------------------------------------------------------------------ #

    def create(
        self,
        *,
        task_id: str,
        repo_name: str,
        repo_path: Path,
        base_branch: str,
        user_request: str,
    ) -> Task:
        """Initialize a run directory and write the initial task.json."""
        run_dir = self.run_dir(task_id)
        if run_dir.exists():
            raise FileExistsError(f"run dir already exists: {run_dir}")
        self.artifacts_dir(task_id).mkdir(parents=True, exist_ok=True)

        task = Task(
            task_id=task_id,
            repo_name=repo_name,
            repo_path=repo_path,
            base_branch=base_branch,
            task_branch=f"agent/{task_id}",
            worktree_path=self.worktree_path(task_id),
            user_request=user_request,
            status=TaskStatus.CREATED,
        )
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        """Persist (or re-persist) task.json. Use after any status change."""
        task.touch()
        self.task_json_path(task.task_id).write_text(
            task.model_dump_json(indent=2)
        )

    def load(self, task_id: str) -> Task:
        return Task.model_validate_json(
            self.task_json_path(task_id).read_text()
        )
