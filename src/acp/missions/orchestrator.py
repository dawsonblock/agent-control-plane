"""Mission orchestrator — automated multi-step mission execution (v0.8.0, Phase 3.1).

The :class:`MissionOrchestrator` runs a mission's steps sequentially, invoking
the ACP workflow for each step. Cross-task artifact sharing is handled via
:func:`acp.missions.store.compute_parent_artifact_hash` — each step's
``evidence.finalized`` event includes the sha256 of the preceding step's
``diff.patch``, cryptographically proving sequential generation.

The orchestrator emits mission-level events (``mission.step_started``,
``mission.step_completed``, ``mission.step_failed``) to the mission's
event log, providing a real-time progress feed for the dashboard UI.

Pause/resume is supported via the mission status:
  - ``running``  — orchestrator proceeds to the next step
  - ``paused``   — orchestrator stops after the current step completes
  - ``completed``/``failed`` — terminal, orchestrator refuses to run

Usage::

    orchestrator = MissionOrchestrator(
        config_path=Path("my.repo.yaml"),
        missions_dir=Path("data/missions"),
        runs_root=Path("data/runs"),
        vault_root=Path("data/vault"),
    )
    orchestrator.run(mission_id="mission_20260626_0001")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from acp.config import load_repo_config
from acp.missions.store import MissionStore, compute_parent_artifact_hash
from acp.models import EventType, Mission, MissionStatus

logger = logging.getLogger(__name__)


class MissionOrchestrator:
    """Runs a mission's steps sequentially via the ACP workflow.

    Each step is a single ``acp run`` invocation with the step's prompt
    as the user request. The orchestrator chains steps by passing the
    parent task's artifact hash to the next step, proving sequential
    generation.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        missions_dir: Path | str = "data/missions",
        runs_root: Path | str = "data/runs",
        vault_root: Path | str = "data/vault",
    ) -> None:
        self.config_path = config_path
        self.store = MissionStore(missions_dir=missions_dir)
        self.runs_root = Path(runs_root)
        self.vault_root = Path(vault_root)

    def run(self, mission_id: str) -> dict[str, Any]:
        """Run all pending steps in a mission sequentially.

        Returns a summary dict with:
          - ``mission_id``: the mission id
          - ``steps_run``: number of steps executed
          - ``steps_passed``: number of steps that passed
          - ``steps_failed``: number of steps that failed
          - ``paused``: True if the mission was paused mid-run
        """
        mission = self.store.load(mission_id)
        if mission.status == MissionStatus.COMPLETED:
            return {
                "mission_id": mission_id,
                "steps_run": 0,
                "steps_passed": 0,
                "steps_failed": 0,
                "paused": False,
                "message": "mission already completed",
            }
        if mission.status == MissionStatus.FAILED:
            return {
                "mission_id": mission_id,
                "steps_run": 0,
                "steps_passed": 0,
                "steps_failed": 0,
                "paused": False,
                "message": "mission failed — cannot run",
            }

        # Mark mission as running.
        mission.status = MissionStatus.RUNNING
        self.store.save(mission)

        events = self.store.events_writer(mission_id)
        steps_run = 0
        steps_passed = 0
        steps_failed = 0
        paused = False

        for idx, step in enumerate(mission.steps):
            # Check for pause — stop after the current step completes.
            if mission.status == MissionStatus.PAUSED:
                paused = True
                break

            # Skip already-completed steps (resume support).
            if step.status == "completed":
                continue
            if step.status == "failed":
                # A previously-failed step blocks the chain.
                events.write(
                    EventType.NODE_FAILED,
                    {
                        "node": "mission_orchestrator",
                        "step_index": idx,
                        "reason": "previous step failed — cannot continue chain",
                    },
                )
                break

            # Determine the parent task id for artifact chaining.
            parent_task_id = ""
            if idx > 0:
                prev_step = mission.steps[idx - 1]
                parent_task_id = prev_step.task_id or ""

            # Emit step_started event.
            step_prompt = step.prompt or step.description
            events.write(
                EventType.MISSION_STEP_STARTED,
                {
                    "mission_id": mission_id,
                    "step_index": idx,
                    "step_prompt": step_prompt[:200],
                    "parent_task_id": parent_task_id,
                },
            )

            step.status = "running"
            self.store.save(mission)

            try:
                result = self._run_step(mission, idx, step_prompt, parent_task_id)
                task_id = result.get("task_id", "")
                step.task_id = task_id
                step.status = "completed" if result.get("status") == "passed" else "failed"
                self.store.save(mission)

                if step.status == "completed":
                    steps_passed += 1
                    events.write(
                        EventType.MISSION_STEP_COMPLETED,
                        {
                            "mission_id": mission_id,
                            "step_index": idx,
                            "task_id": task_id,
                            "parent_artifact_hash": result.get("parent_artifact_hash"),
                        },
                    )
                else:
                    steps_failed += 1
                    events.write(
                        EventType.MISSION_STEP_FAILED,
                        {
                            "mission_id": mission_id,
                            "step_index": idx,
                            "task_id": task_id,
                            "error": result.get("error", "step did not pass"),
                        },
                    )
                    # A failed step stops the chain.
                    mission.status = MissionStatus.FAILED
                    self.store.save(mission)
                    break

                steps_run += 1

            except Exception as exc:  # noqa: BLE001
                step.status = "failed"
                self.store.save(mission)
                steps_failed += 1
                steps_run += 1
                events.write(
                    EventType.MISSION_STEP_FAILED,
                    {
                        "mission_id": mission_id,
                        "step_index": idx,
                        "error": str(exc),
                    },
                )
                mission.status = MissionStatus.FAILED
                self.store.save(mission)
                break

        # Finalize mission status.
        if not paused and mission.status == MissionStatus.RUNNING:
            all_done = all(s.status in ("completed", "failed") for s in mission.steps)
            if all_done and steps_failed == 0:
                mission.status = MissionStatus.COMPLETED
                events.write(EventType.MISSION_COMPLETED, {"mission_id": mission_id})
            elif steps_failed > 0:
                mission.status = MissionStatus.FAILED
                events.write(
                    EventType.MISSION_FAILED,
                    {"mission_id": mission_id},
                )
            self.store.save(mission)

        return {
            "mission_id": mission_id,
            "steps_run": steps_run,
            "steps_passed": steps_passed,
            "steps_failed": steps_failed,
            "paused": paused,
        }

    def _run_step(
        self,
        mission: Mission,
        step_index: int,
        prompt: str,
        parent_task_id: str,
    ) -> dict[str, Any]:
        """Run a single mission step via the ACP workflow.

        Returns a dict with ``task_id``, ``status``, ``parent_artifact_hash``,
        and optionally ``error``.
        """
        from acp.graph.workflow import run_workflow

        cfg = load_repo_config(self.config_path)

        # Compute the parent artifact hash for cross-task binding.
        parent_hash = compute_parent_artifact_hash(
            runs_root=self.runs_root,
            parent_task_id=parent_task_id,
        )

        result = run_workflow(
            config=cfg,
            user_request=prompt,
            runs_root=self.runs_root,
            vault_root=self.vault_root,
            mission_id=mission.mission_id,
            mission_step_index=step_index,
            parent_task_id=parent_task_id,
        )

        task_id = result.get("task_id", "")
        status = result.get("status", "failed")
        if hasattr(status, "value"):
            status = status.value

        return {
            "task_id": task_id,
            "status": status,
            "parent_artifact_hash": parent_hash,
            "error": result.get("error"),
        }

    def run_single_step(self, mission_id: str, step_index: int) -> dict[str, Any]:
        """Run a single step by index without running the full mission.

        This enables per-step "Play" buttons in the dashboard UI (M15).
        The step must be in a non-terminal state (``pending`` or ``failed``)
        and the mission must not be in a terminal state (``completed`` or
        ``failed``).

        Returns a dict with:
          - ``mission_id``: the mission id
          - ``step_index``: the step index that was run
          - ``task_id``: the task id produced by the workflow
          - ``status``: the step status after running (``completed`` or ``failed``)
          - ``error``: error message if the step failed, otherwise ``None``
        """
        mission = self.store.load(mission_id)
        if mission is None:
            raise FileNotFoundError(f"mission {mission_id} not found")
        if mission.status in (MissionStatus.COMPLETED, MissionStatus.FAILED):
            raise ValueError(f"mission {mission_id} is {mission.status.value} — cannot run step")
        if step_index < 0 or step_index >= len(mission.steps):
            raise IndexError(f"step index {step_index} out of range (0..{len(mission.steps) - 1})")

        step = mission.steps[step_index]
        if step.status not in ("pending", "failed"):
            raise ValueError(
                f"step {step_index} is '{step.status}' — only pending or failed steps can be run"
            )

        events = self.store.events_writer(mission_id)
        step_prompt = step.prompt or step.description

        # Determine the parent task id for artifact chaining.
        parent_task_id = ""
        if step_index > 0:
            prev_step = mission.steps[step_index - 1]
            parent_task_id = prev_step.task_id or ""

        events.write(
            EventType.MISSION_STEP_STARTED,
            {
                "mission_id": mission_id,
                "step_index": step_index,
                "step_prompt": step_prompt[:200],
                "parent_task_id": parent_task_id,
            },
        )

        # Mark mission as running so pause() works during execution.
        prev_mission_status = mission.status
        mission.status = MissionStatus.RUNNING
        step.status = "running"
        self.store.save(mission)

        try:
            result = self._run_step(mission, step_index, step_prompt, parent_task_id)
            task_id = result.get("task_id", "")
            step.task_id = task_id
            step.status = "completed" if result.get("status") == "passed" else "failed"
            self.store.save(mission)

            if step.status == "completed":
                events.write(
                    EventType.MISSION_STEP_COMPLETED,
                    {
                        "mission_id": mission_id,
                        "step_index": step_index,
                        "task_id": task_id,
                        "parent_artifact_hash": result.get("parent_artifact_hash"),
                    },
                )
            else:
                events.write(
                    EventType.MISSION_STEP_FAILED,
                    {
                        "mission_id": mission_id,
                        "step_index": step_index,
                        "task_id": task_id,
                        "error": result.get("error", "step did not pass"),
                    },
                )

            # Finalize mission status: if all steps are done, update.
            all_done = all(s.status in ("completed", "failed") for s in mission.steps)
            if all_done:
                any_failed = any(s.status == "failed" for s in mission.steps)
                mission.status = MissionStatus.FAILED if any_failed else MissionStatus.COMPLETED
                if mission.status == MissionStatus.COMPLETED:
                    events.write(
                        EventType.MISSION_COMPLETED,
                        {"mission_id": mission_id},
                    )
                else:
                    events.write(
                        EventType.MISSION_FAILED,
                        {"mission_id": mission_id},
                    )
                self.store.save(mission)
            elif mission.status == MissionStatus.RUNNING:
                # Not all steps done — restore to a non-running state.
                # If the mission was already RUNNING (e.g. via run()), keep
                # it RUNNING so the orchestrator loop can continue. Otherwise
                # (was PENDING), set to PAUSED since a step has executed but
                # the mission is not actively being driven by run().
                if prev_mission_status == MissionStatus.RUNNING:
                    pass  # keep RUNNING
                else:
                    mission.status = MissionStatus.PAUSED
                    self.store.save(mission)

            return {
                "mission_id": mission_id,
                "step_index": step_index,
                "task_id": task_id,
                "status": step.status,
                "error": result.get("error"),
            }

        except Exception as exc:  # noqa: BLE001
            step.status = "failed"
            self.store.save(mission)
            events.write(
                EventType.MISSION_STEP_FAILED,
                {
                    "mission_id": mission_id,
                    "step_index": step_index,
                    "error": str(exc),
                },
            )
            # Finalize mission status on exception too.
            all_done = all(s.status in ("completed", "failed") for s in mission.steps)
            if all_done:
                mission.status = MissionStatus.FAILED
                events.write(
                    EventType.MISSION_FAILED,
                    {"mission_id": mission_id},
                )
                self.store.save(mission)
            elif prev_mission_status == MissionStatus.RUNNING:
                mission.status = MissionStatus.RUNNING
                self.store.save(mission)
            else:
                mission.status = MissionStatus.PAUSED
                self.store.save(mission)

            return {
                "mission_id": mission_id,
                "step_index": step_index,
                "task_id": "",
                "status": "failed",
                "error": str(exc),
            }

    def pause(self, mission_id: str) -> None:
        """Pause a running mission — stops after the current step completes."""
        mission = self.store.load(mission_id)
        if mission.status != MissionStatus.RUNNING:
            raise ValueError(f"mission {mission_id} is not running (status={mission.status})")
        mission.status = MissionStatus.PAUSED
        self.store.save(mission)

    def resume(self, mission_id: str) -> None:
        """Resume a paused mission."""
        mission = self.store.load(mission_id)
        if mission.status != MissionStatus.PAUSED:
            raise ValueError(f"mission {mission_id} is not paused (status={mission.status})")
        mission.status = MissionStatus.RUNNING
        self.store.save(mission)
