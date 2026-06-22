"""Report templates — pure-string rendering, no template-engine dependency.

Kept Jinja2-free for v0.1 to minimize deps. The body of ``final_report.md``
is assembled here from the typed models. The Obsidian note is this same body
prepended with lifecycle frontmatter (see vault/obsidian_writer.py).
"""

from __future__ import annotations

from acp.gitops.diff import DiffCapture
from acp.models import (
    AgentResult,
    CommandResult,
    ReviewResult,
    Task,
    TaskStatus,
)
from acp.testing.parsers import summarize

_PASS = "✅ pass"
_FAIL = "❌ fail"
_SKIP = "⏭ skip"


def render_report(
    *,
    task: Task,
    command_results: list[CommandResult],
    review: ReviewResult,
    diff: DiffCapture,
    agent_result: AgentResult | None,
    repair_history: list[dict[str, object]] | None = None,
) -> str:
    """Render the full final_report.md body (no frontmatter)."""
    summary = summarize(command_results)
    status_word = _status_word(task.status)
    repair_history = repair_history or []

    lines: list[str] = []
    lines.append(f"# Task report: {task.task_id}")
    lines.append("")
    lines.append(f"- **Status:** {status_word}")
    lines.append(f"- **Repo:** `{task.repo_name}` (`{task.base_branch}` → `{task.task_branch}`)")
    lines.append(f"- **Request:** {task.user_request}")
    lines.append(f"- **Risk:** {review.risk.value}  —  **Recommendation:** {review.recommendation.value}")
    lines.append(f"- **Diff:** {len(diff.changed_files)} file(s), +{diff.insertions}/-{diff.deletions}")
    lines.append(f"- **Commands:** {summary['total']} ran, {len(summary['failed'])} failed, {len(summary['skipped'])} skipped")  # type: ignore[arg-type]
    if repair_history:
        lines.append(f"- **Repair attempts:** {len(repair_history)}")
    lines.append(f"- **Created:** {task.created_at}")
    lines.append("")

    lines.append("## Reviewer summary")
    lines.append("")
    lines.append(review.summary or "_(no summary)_")
    if review.concerns:
        lines.append("")
        lines.append("### Concerns")
        lines.append("")
        for c in review.concerns:
            lines.append(f"- {c}")
    lines.append("")

    lines.append("## Changed files")
    lines.append("")
    if diff.changed_files:
        for f in review.changed_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines.append("## Commands")
    lines.append("")
    lines.append("| command | exit | result | duration (s) |")
    lines.append("| --- | --- | --- | --- |")
    for r in command_results:
        if r.skipped:
            outcome = _SKIP
        elif r.passed:
            outcome = _PASS
        else:
            outcome = _FAIL
        lines.append(
            f"| `{r.command or '—'}` | {r.exit_code} | {outcome} | {r.duration_seconds} |"
        )
    lines.append("")

    if repair_history:
        lines.append("## Repair attempts")
        lines.append("")
        lines.append("Tests failed and the control plane re-ran the agent with a repair prompt:")
        lines.append("")
        for h in repair_history:
            attempt = h.get("attempt", "?")
            prompt = str(h.get("prompt_path", "")).split("/")[-1]
            lines.append(f"- attempt {attempt} — prompt: `artifacts/{prompt}`")
        lines.append("")

    lines.append("## Agent")
    lines.append("")
    if agent_result is not None:
        lines.append(f"- **Agent:** `{agent_result.agent_name}` (exit {agent_result.exit_code})")
        lines.append(f"- **Summary:** {agent_result.summary}")
        lines.append(f"- **stdout:** `artifacts/agent_stdout.txt`")
        lines.append(f"- **stderr:** `artifacts/agent_stderr.txt`")
    else:
        lines.append("_(no agent result — run failed before agent execution)_")
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    lines.append("All artifacts live under `data/runs/<task_id>/artifacts/`:")
    lines.append("")
    lines.append("- `agent_prompt.txt` — what the agent was told")
    lines.append("- `agent_stdout.txt` / `agent_stderr.txt` — agent output")
    lines.append("- `<cmd>_stdout.txt` / `<cmd>_stderr.txt` — per-command output")
    lines.append("- `commands.json` — command results table")
    lines.append("- `diff.patch` / `diff_stat.txt` — the captured change")
    lines.append("- `review.json` — this review, machine-readable")
    lines.append("")
    lines.append("The event log (`events.jsonl`) is the source of truth; this report is its human-readable projection.")
    lines.append("")

    return "\n".join(lines)


def _status_word(status: TaskStatus) -> str:
    if status == TaskStatus.PASSED:
        return "✅ passed"
    if status == TaskStatus.FAILED:
        return "❌ failed"
    return status.value
