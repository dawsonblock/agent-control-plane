"""Report writer — assembles and persists final_report.md.

The report is the human-readable projection of the event log + captured
artifacts. It is *truth*, in the sense that it only ever reflects what
actually happened (never what was intended). The Obsidian note is a copy
of this file with lifecycle frontmatter prepended.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from acp.gitops.diff import DiffCapture
from acp.models import AgentResult, CommandResult, Event, ReviewResult, Task
from acp.reports.templates import render_failure_report, render_report
from acp.review.gates import GateResult

logger = logging.getLogger(__name__)


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
    manifest_hash: str | None = None,
) -> Path:
    """Render a minimal final_report.md for early failures (no diff/review).

    Used by ``failed_node`` when the task failed before producing a diff or
    review (dirty repo, worktree error, node crash). The spec rule is "a
    failed task produces an evidence report" — this ensures that's true even
    for early failures. When ``events`` is provided, an event timeline is
    included so the report shows what happened before the failure. When
    ``manifest_hash`` is provided, it's included so the report ↔ evidence
    binding is verifiable.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    body = render_failure_report(task=task, error=error, events=events, manifest_hash=manifest_hash)
    report_path = artifact_dir / "final_report.md"
    report_path.write_text(body)
    return report_path


def rerender_report_from_run(run_dir: Path) -> Path | None:
    """Re-render final_report.md from the on-disk run state.

    Used after lifecycle events (approve/reject) to ensure the report's event
    timeline + manifest hash reflect the latest event log. Reads the existing
    report, task.json, events.jsonl, and evidence_manifest.json from the run
    directory and re-renders.

    Returns the report path, or ``None`` if the run directory doesn't have
    the necessary files (e.g. early-failure runs without a full report).
    """

    from acp.models import Event, Task

    run_dir = Path(run_dir)
    report_path = run_dir / "artifacts" / "final_report.md"
    if not report_path.is_file():
        return None  # no report to re-render

    # Read the manifest hash from the current manifest.
    manifest_path = run_dir / "evidence_manifest.json"
    manifest_hash = None
    if manifest_path.is_file():
        try:
            manifest_hash = json.loads(manifest_path.read_text()).get("manifest_hash")
        except Exception as exc:
            logger.warning("failed to read manifest hash: %s", exc)

    # Read the updated event log.
    events_path = run_dir / "events.jsonl"
    events: list[Event] = []
    if events_path.is_file():
        for line in events_path.read_text().splitlines():
            if line.strip():
                try:
                    events.append(Event.model_validate_json(line))
                except Exception as exc:
                    logger.warning("skipping malformed event line in report: %s", exc)

    # Read task.json for the task object.
    task_json_path = run_dir / "task.json"
    if not task_json_path.is_file():
        return None
    task = Task.model_validate_json(task_json_path.read_text())

    # Determine if this is a failure report or a full report by checking
    # whether the existing report contains the failure marker.
    existing = report_path.read_text()
    if "## Failure" in existing or "task.failed" in existing and "## Review" not in existing:
        # It's a failure report — re-render as failure report.
        error_line = "Task failed (see event log for details)."
        body = render_failure_report(
            task=task, error=error_line, events=events, manifest_hash=manifest_hash
        )
    else:
        # It's a full report — we can't fully reconstruct it without the
        # review/diff/agent_result, but we can append a lifecycle note.
        # The simplest correct approach: append a Lifecycle Events section
        # to the existing report with the updated event timeline + manifest hash.
        import re

        # Update the manifest hash in the Evidence section.
        if "**Evidence manifest hash:**" in existing and manifest_hash:
            existing = re.sub(
                r"\*\*Evidence manifest hash:\*\* `[a-f0-9]+`",
                f"**Evidence manifest hash:** `{manifest_hash}`",
                existing,
            )
        # Replace the event timeline if present.
        if "## Event timeline" in existing and events:
            # Build the new timeline table.
            timeline_lines = [
                f"The complete event log ({len(events)} events, hash-chained):",
                "",
                "| # | event_id | type | timestamp | hash (first 12) |",
                "| --- | --- | --- | --- | --- |",
            ]
            for i, e in enumerate(events, 1):
                timeline_lines.append(
                    f"| {i} | {e.event_id} | {e.type.value} | {e.timestamp} | {e.hash[:12]} |"
                )
            timeline_table = "\n".join(timeline_lines)
            existing = re.sub(
                r"## Event timeline\n.*?(?=\n## |\Z)",
                f"## Event timeline\n{timeline_table}\n",
                existing,
                flags=re.DOTALL,
            )
        body = existing

    report_path.write_text(body)
    return report_path
