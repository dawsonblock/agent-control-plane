"""Mission store — directory layout, ID generation, and persistence.

Owns the layout under ``data/missions/`` and the monotonic mission-id
generator. A mission id is ``mission_<YYYYMMDD>_<NNNN>`` where the
sequence restarts each UTC day, mirroring the task-id scheme so ids
sort chronologically and are human-readable.

Each mission lives in ``<missions_dir>/<mission_id>/`` containing:
  - ``mission.yaml`` — canonical mission state (the :class:`Mission` model
    serialized as YAML).
  - ``events.jsonl`` — mission-level event log (``mission.created``,
    ``mission.completed``) using the same hash-chained :class:`EventWriter`
    as task runs.

The mission event log uses the mission_id as the ``task_id`` field in
events. This is safe because mission events live in their own directory
under ``data/missions/``, never under ``data/runs/``, so ``acp verify``
(which operates on task run dirs) is unaffected.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from acp.events import EventWriter
from acp.models import EventType, Mission, MissionStatus, _utcnow_iso

# mission_20260626_0001
_MISSION_ID_RE = re.compile(r"^mission_(\d{8})_(\d{4})$")


def is_valid_mission_id(mission_id: str) -> bool:
    """Whether ``mission_id`` matches ``mission_<YYYYMMDD>_<NNNN>``.

    Used to gate every CLI command that turns a user-supplied mission id
    into a filesystem path, preventing path traversal (same rationale as
    :func:`acp.store.is_valid_task_id`).
    """
    return bool(_MISSION_ID_RE.match(mission_id))


class MissionStore:
    """Creates and reads mission directories and mission.yaml files.

    Args:
        missions_dir: Directory holding one subdir per mission. Defaults
            to ``data/missions`` relative to cwd.
    """

    def __init__(self, missions_dir: str | Path | None = None) -> None:
        self.root = Path(missions_dir or "data/missions").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # IDs
    # ------------------------------------------------------------------ #

    def next_mission_id(self, *, now: datetime | None = None) -> str:
        """Return the next mission id for today.

        Scans existing mission dirs for today's sequence number so ids
        are unique and monotonic within a day.
        """
        today = (now or datetime.now(UTC)).strftime("%Y%m%d")
        prefix = f"mission_{today}_"
        seq = 0
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith(prefix):
                m = _MISSION_ID_RE.match(child.name)
                if m and int(m.group(2)) > seq:
                    seq = int(m.group(2))
        return f"{prefix}{seq + 1:04d}"

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def mission_dir(self, mission_id: str) -> Path:
        return self.root / mission_id

    def mission_yaml_path(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "mission.yaml"

    def events_path(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "events.jsonl"

    # ------------------------------------------------------------------ #
    # Create / read / update
    # ------------------------------------------------------------------ #

    def create(
        self,
        *,
        mission_id: str,
        goal: str,
        repo_name: str,
        repo_path: Path,
        base_branch: str = "main",
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
    ) -> Mission:
        """Initialize a mission directory, write mission.yaml, and emit mission.created.

        Args:
            steps: Optional list of step dicts (``{"description": "..."}``).
                If omitted, the mission starts with no steps — use
                :meth:`add_step` or the CLI ``acp mission split`` command
                to populate them.

        Returns the created :class:`Mission`.
        """
        mission_dir = self.mission_dir(mission_id)
        if mission_dir.exists():
            raise FileExistsError(f"mission dir already exists: {mission_dir}")
        mission_dir.mkdir(parents=True, exist_ok=True)

        from acp.models import MissionStep

        mission = Mission(
            mission_id=mission_id,
            goal=goal,
            description=description,
            repo_name=repo_name,
            repo_path=repo_path,
            base_branch=base_branch,
            steps=[MissionStep(**s) for s in (steps or [])],
            status=MissionStatus.CREATED,
        )
        self.save(mission)

        # Emit mission.created event to the mission's own event log.
        events = EventWriter(mission_id, mission_dir)
        events.write(
            EventType.MISSION_CREATED,
            {
                "mission_id": mission_id,
                "goal": goal,
                "repo_name": repo_name,
                "step_count": len(mission.steps),
            },
        )
        return mission

    def save(self, mission: Mission) -> None:
        """Persist (or re-persist) mission.yaml. Call after any state change."""
        mission.touch()
        # Serialize with YAML for human readability (matching repo configs).
        data = mission.model_dump(mode="json")
        self.mission_yaml_path(mission.mission_id).write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False)
        )

    def load(self, mission_id: str) -> Mission:
        """Load a mission by id from its mission.yaml."""
        return Mission.model_validate(
            yaml.safe_load(self.mission_yaml_path(mission_id).read_text())
        )

    def list_missions(self) -> list[Mission]:
        """List all missions, ordered by creation (mission_id sort)."""
        missions: list[Mission] = []
        for child in sorted(self.root.iterdir()):
            yaml_path = child / "mission.yaml"
            if not yaml_path.is_file():
                continue
            try:
                missions.append(Mission.model_validate(yaml.safe_load(yaml_path.read_text())))
            except Exception:  # noqa: BLE001
                pass  # skip malformed
        return missions

    # ------------------------------------------------------------------ #
    # Step management
    # ------------------------------------------------------------------ #

    def add_step(self, mission_id: str, description: str) -> Mission:
        """Append a step to a mission and persist."""
        from acp.models import MissionStep

        mission = self.load(mission_id)
        mission.steps.append(MissionStep(description=description))
        self.save(mission)
        return mission

    def mark_step_running(self, mission_id: str, step_index: int, task_id: str) -> Mission:
        """Mark a step as running and record the spawned task_id."""
        mission = self.load(mission_id)
        if step_index < 0 or step_index >= len(mission.steps):
            raise IndexError(f"step_index {step_index} out of range (0..{len(mission.steps) - 1})")
        mission.steps[step_index].status = "running"
        mission.steps[step_index].task_id = task_id
        if mission.status == MissionStatus.CREATED:
            mission.status = MissionStatus.IN_PROGRESS
        self.save(mission)
        return mission

    def mark_step_completed(self, mission_id: str, step_index: int, success: bool) -> Mission:
        """Mark a step as completed or failed."""
        mission = self.load(mission_id)
        if step_index < 0 or step_index >= len(mission.steps):
            raise IndexError(f"step_index {step_index} out of range (0..{len(mission.steps) - 1})")
        mission.steps[step_index].status = "completed" if success else "failed"
        self.save(mission)
        return mission

    # ------------------------------------------------------------------ #
    # Completion
    # ------------------------------------------------------------------ #

    def complete(self, mission_id: str) -> Mission:
        """Mark a mission as completed and emit mission.completed event.

        A mission is completable when all steps are in a terminal state
        (``completed`` or ``failed``). If any step is still ``pending`` or
        ``running``, raises ``ValueError``.
        """
        mission = self.load(mission_id)
        non_terminal = [
            i for i, s in enumerate(mission.steps) if s.status not in ("completed", "failed")
        ]
        if non_terminal:
            raise ValueError(
                f"mission {mission_id} has non-terminal steps at index "
                f"{non_terminal}. All steps must be completed or failed."
            )
        mission.status = MissionStatus.COMPLETED
        mission.completed_at = _utcnow_iso()
        self.save(mission)

        events = EventWriter(mission_id, self.mission_dir(mission_id))
        events.write(
            EventType.MISSION_COMPLETED,
            {
                "mission_id": mission_id,
                "step_count": len(mission.steps),
                "completed_steps": sum(1 for s in mission.steps if s.status == "completed"),
                "failed_steps": sum(1 for s in mission.steps if s.status == "failed"),
            },
        )
        return mission

    # ------------------------------------------------------------------ #
    # Cross-task artifact sharing (Phase 5.2)
    # ------------------------------------------------------------------ #

    def get_parent_task_id(self, mission_id: str, step_index: int) -> str:
        """Return the task_id of the preceding mission step.

        For step 0, returns "" (no parent). For step N > 0, returns the
        task_id recorded on step N-1 (or "" if that step hasn't been
        spawned yet).
        """
        if step_index <= 0:
            return ""
        mission = self.load(mission_id)
        if step_index >= len(mission.steps):
            return ""
        return mission.steps[step_index - 1].task_id


def compute_parent_artifact_hash(
    runs_root: Path | str,
    parent_task_id: str,
) -> str | None:
    """Compute the sha256 of a parent task's diff.patch artifact.

    This is the cross-task artifact binding (Phase 5.2): when Task B is
    spawned as the next step in a mission, its ``evidence.finalized``
    event includes this hash, cryptographically proving that Task B was
    generated with knowledge of Task A's output — even before Task A is
    merged to main.

    Returns ``None`` if the parent task's diff.patch doesn't exist (e.g.
    the parent task failed before capturing a diff, or the task_id is
    empty).
    """
    if not parent_task_id:
        return None
    diff_patch = Path(runs_root) / parent_task_id / "artifacts" / "diff.patch"
    if not diff_patch.is_file():
        return None
    h = hashlib.sha256()
    with diff_patch.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
