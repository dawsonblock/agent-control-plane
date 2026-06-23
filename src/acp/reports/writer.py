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
from acp.models import AgentResult, CommandResult, ReviewResult, Task
from acp.reports.templates import render_report
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
) -> Path:
    """Render final_report.md into ``artifact_dir`` and return its path.

    When ``gate_result`` is provided, passes it through to the template so
    the Gate Summary section renders from the authoritative GateResult.
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
    )
    report_path = artifact_dir / "final_report.md"
    report_path.write_text(body)
    return report_path
