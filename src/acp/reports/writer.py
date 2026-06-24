"""Report writer — assembles and persists final_report.md.

The report is the human-readable projection of the event log + captured
artifacts. It is *truth*, in the sense that it only ever reflects what
actually happened (never what was intended). The Obsidian note is a copy
of this file with lifecycle frontmatter prepended.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.gitops.diff import DiffCapture
from acp.models import AgentResult, CommandResult, Event, ReviewResult, Task
from acp.reports.templates import render_failure_report, render_report
from acp.review.gates import GateResult


def write_report(
    *,
    task: Task,
    command_results: list[CommandResult],
    review: ReviewResult,
    diff: DiffCapture,
    artifact_dir: Path,
    agent_result: AgentResult | None = None,
    repair_history: list[dict[str, Any]] | None = None,
    gate_result: GateResult | None = None,
    manifest_hash: str | None = None,
    events: list[Event] | None = None,
) -> Path:
    """Render final_report.md into ``artifact_dir`` and return its path.

    When ``gate_result`` is provided, passes it through to the template so
    the Gate Summary section renders from the authoritative GateResult.
    When ``manifest_hash`` is provided, it's included in the Evidence section
    so the report ↔ evidence binding is verifiable.
    When ``events`` is provided, an Event Timeline section is rendered,
    making the report a true projection of the event log.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    body = render_report(
        task=task,
        command_results=command_results,
        review=review,
        diff=diff,
        agent_result=agent_result,
        repair_history=repair_history,
        gate_result=gate_result,
        manifest_hash=manifest_hash,
        events=events,
    )
    report_path = artifact_dir / "final_report.md"
    report_path.write_text(body)
    return report_path


def write_failure_report(
    *,
    task: Task,
    error: str,
    artifact_dir: Path,
    events: list[Event] | None = None,
) -> Path:
    """Render a minimal final_report.md for early failures (no diff/review).

    Used by ``failed_node`` when the task failed before producing a diff or
    review (dirty repo, worktree error, node crash). The spec rule is "a
    failed task produces an evidence report" — this ensures that's true even
    for early failures. When ``events`` is provided, an event timeline is
    included so the report shows what happened before the failure.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    body = render_failure_report(task=task, error=error, events=events)
    report_path = artifact_dir / "final_report.md"
    report_path.write_text(body)
    return report_path
