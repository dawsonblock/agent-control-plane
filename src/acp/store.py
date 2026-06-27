"""Task store — run-directory and task.json persistence.

Owns the layout under ``data/runs/`` and the monotonic task-id generator.
A task id is ``task_<YYYYMMDD>_<NNNN>`` where the sequence restarts each
UTC day, so ids sort chronologically and are human-readable.

v0.7.0 (Phase 1.1): When ``durable_store`` is provided and
``primary="sqlite"``, the SQLite store becomes the primary source of
truth for task state. task.json files are still written (as a projection
for backwards compatibility and evidence hashing), but reads come from
SQLite first, falling back to JSON if the task isn't in the database.
This enables gradual migration — operators can flip the flag per-repo.
"""

from __future__ import annotations

import fcntl
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from acp.models import Task, TaskStatus

if TYPE_CHECKING:
    from acp.evidence.durable_task_store import DurableTaskStore

# File-based locking for atomic task-id allocation. Prevents two concurrent
# processes (e.g., the API server handling parallel requests) from generating
# the same task id. Uses fcntl.flock on Unix (Mac-first project).

# task_20260621_0001
_TASK_ID_RE = re.compile(r"^task_(\d{8})_(\d{4})$")


def is_valid_task_id(task_id: str) -> bool:
    """Whether ``task_id`` matches the canonical ``task_<YYYYMMDD>_<NNNN>`` shape.

    Used to gate every CLI command that turns a user-supplied task id into a
    filesystem path (``self.root / task_id``). A local control plane that
    manipulates files must not accept path-shaped ids — ``..``, absolute paths,
    or nested segments could escape the runs root.
    """
    return bool(_TASK_ID_RE.match(task_id))


def _highest_branch_seq(repo_path: Path, prefix: str) -> int:
    """Highest sequence number among ``agent/<prefix*>`` branches in a repo.

    Used to keep task ids unique across runs-roots that share a repo, so the
    derived branch name ``agent/<task_id>`` can't collide. Returns 0 if none.
    """
    try:
        from git import Repo

        repo = Repo(str(repo_path))
    except Exception:  # noqa: BLE001
        return 0  # not a git repo or unreadable — caller will fail more usefully
    # Branch names look like "agent/task_20260622_0001".
    seq = 0
    for head in repo.heads:
        name = head.name
        if name.startswith("agent/"):
            tail = name[len("agent/") :]
            m = _TASK_ID_RE.match(tail)
            if m and tail.startswith(prefix) and int(m.group(2)) > seq:
                seq = int(m.group(2))
    return seq


class TaskStore:
    """Creates and reads task run directories and task.json files.

    Args:
        runs_root: Directory holding one subdir per task. Defaults to
            ``data/runs`` relative to cwd.
        durable_store: Optional :class:`DurableTaskStore` for SQLite
            dual-writing (v0.7.0 Phase 1.1). When provided, ``save()``
            writes to both JSON and SQLite.
        primary: Which store is primary for reads — ``"json"`` (default)
            or ``"sqlite"``. When ``"sqlite"``, ``load()`` reads from
            SQLite first, falling back to JSON.
    """

    def __init__(
        self,
        runs_root: str | Path | None = None,
        *,
        durable_store: DurableTaskStore | None = None,
        primary: str = "json",
    ) -> None:
        self.root = Path(runs_root or "data/runs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._durable = durable_store
        self._primary = primary

    # ------------------------------------------------------------------ #
    # IDs
    # ------------------------------------------------------------------ #

    def next_task_id(
        self,
        *,
        now: datetime | None = None,
        repo_path: Path | None = None,
    ) -> str:
        """Return the next task id for today.

        Scans existing run dirs for today's sequence number. If ``repo_path``
        is given, also scans that repo's ``agent/task_*`` branches so two
        runs-roots pointed at the same repo can't collide on a branch name.

        Uses a file lock (``flock``) on a lock file in the runs root to
        prevent two concurrent processes from generating the same id.
        """
        today = (now or datetime.now(UTC)).strftime("%Y%m%d")
        prefix = f"task_{today}_"
        lock_path = self.root / ".next_id_lock"
        # Atomically create + lock. The lock file persists (harmless).
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                seq = 0
                for child in self.root.iterdir():
                    if child.is_dir() and child.name.startswith(prefix):
                        m = _TASK_ID_RE.match(child.name)
                        if m and int(m.group(2)) > seq:
                            seq = int(m.group(2))
                if repo_path is not None:
                    seq = max(seq, _highest_branch_seq(repo_path, prefix))
                return f"{prefix}{seq + 1:04d}"
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

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
        recursion_depth: int = 0,
    ) -> Task:
        """Initialize a run directory and write the initial task.json."""
        run_dir = self.run_dir(task_id)
        # v0.7.4: Atomic existence check via mkdir instead of TOCTOU-vulnerable
        # exists() + mkdir() pattern.
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise FileExistsError(f"run dir already exists: {run_dir}") from None
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
            recursion_depth=recursion_depth,
        )
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        """Persist (or re-persist) task.json. Use after any status change.

        v0.7.0: When a durable store is configured, also writes to SQLite.
        """
        task.touch()
        self.task_json_path(task.task_id).write_text(task.model_dump_json(indent=2))
        if self._durable is not None:
            self._durable.save(task)

    def load(self, task_id: str) -> Task:
        """Load a task by id.

        v0.7.0: When primary="sqlite" and a durable store is configured,
        reads from SQLite first, falling back to JSON if not found.
        When primary="json" (default), reads from JSON as before.
        """
        if self._primary == "sqlite" and self._durable is not None:
            task = self._durable.load(task_id)
            if task is not None:
                return task
            # Fall back to JSON if not in SQLite (e.g., pre-migration task).
        return Task.model_validate_json(self.task_json_path(task_id).read_text())
