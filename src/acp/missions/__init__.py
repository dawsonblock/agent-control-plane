"""Mission layer (v0.7.0 / M14) — group sequential tasks into larger epics.

A mission is an overarching goal (e.g. "Migrate to React 19") that ACP
splits into ordered :class:`~acp.models.MissionStep` entries. Each step
becomes a single ACP task run. The mission directory
``data/missions/<mission_id>/`` holds ``mission.yaml`` (canonical state)
and ``events.jsonl`` (mission-level event log).

Evidence:
  - ``mission.created`` — written when a mission is defined from a goal.
  - ``mission.completed`` — written when all steps reach a terminal state.
"""

from __future__ import annotations

from acp.missions.store import MissionStore

__all__ = ["MissionStore"]
