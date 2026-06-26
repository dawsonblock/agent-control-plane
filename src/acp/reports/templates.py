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
    Event,
    Recommendation,
    ReviewResult,
    Task,
    TaskStatus,
)
from acp.review.gates import GateResult
from acp.testing.parsers import summarize


_PASS = "\u2705 pass"
_FAIL = "\u274c fail"
_SKIP = "\u23ed skip"


def render_report(
    *,
    task: Task,
    command_results: list[CommandResult],
    review: ReviewResult,
    diff: DiffCapture,
    agent_result: AgentResult | None,
    repair_history: list[dict[str, object]] | None = None,
    gate_result: GateResult | None = None,
    manifest_hash: str | None = None,
    events: list[Event] | None = None,
) -> str:
    """Render the full final_report.md body (no frontmatter).

    When ``gate_result`` is provided, the Gate Summary section is rendered
    from the gate result's reasons rather than recomputed from raw data.
    This ensures the report always agrees with ``evaluate_final_gates``.

    When ``events`` is provided, an Event Timeline section is rendered
    showing the full event sequence — making the report a true projection
    of the event log, not just a reference to it.
    """
    summary = summarize(command_results)
    status_word = _status_word(task.status)
    repair_history = repair_history or []

    lines: list[str] = []
    lines.append(f"# Task report: {task.task_id}")
    lines.append("")
    lines.append(f"- **Status:** {status_word}")
    lines.append(f"- **Repo:** `{task.repo_name}` (`{task.base_branch}` -> `{task.task_branch}`)")
    lines.append(f"- **Base commit:** `{task.base_commit_sha or '(not recorded)'}`")
    lines.append(f"- **Worktree:** `{task.worktree_path}`")
    lines.append(f"- **Request:** {task.user_request}")
    lines.append(f"- **Risk:** {review.risk.value}  --  **Recommendation:** {review.recommendation.value}")
    lines.append(f"- **Diff:** {len(diff.changed_files)} file(s), +{diff.insertions}/-{diff.deletions}")
    lines.append(f"- **Commands:** {summary['total']} ran, {len(summary['failed'])} failed, {len(summary['skipped'])} skipped")
    if repair_history:
        lines.append(f"- **Repair attempts:** {len(repair_history)}")
    lines.append(f"- **Created:** {task.created_at}")
    lines.append("")
    lines.append("## Gate Summary")
    lines.append("")
    lines.append("| Gate | Result | Evidence |")
    lines.append("| --- | --- | --- |")

    if gate_result is not None:
        # Render from the authoritative GateResult.
        gr = gate_result
        if gr.agent_exit_code is None:
            lines.append("| Agent exit code | ❌ failed | missing |")
        elif gr.agent_exit_code != 0:
            lines.append(f"| Agent exit code | ❌ failed | exit_code={gr.agent_exit_code} |")
        else:
            lines.append(f"| Agent exit code | ✅ passed | exit_code={gr.agent_exit_code} |")

        if gr.validation_commands_ran == 0:
            lines.append("| Validation commands ran | 🔶 needs review | 0 commands ran |")
        else:
            lines.append(f"| Validation commands ran | ✅ passed | {gr.validation_commands_ran} commands ran |")

        if gr.validation_commands_failed > 0:
            passed = gr.validation_commands_ran - gr.validation_commands_failed
            lines.append(f"| Validation commands passed | ❌ failed | {passed}/{gr.validation_commands_ran} passed |")
        elif gr.validation_commands_ran > 0:
            lines.append(f"| Validation commands passed | ✅ passed | {gr.validation_commands_ran}/{gr.validation_commands_ran} passed |")
        else:
            lines.append("| Validation commands passed | 🔶 needs review | n/a |")

        if gr.diff_is_empty:
            lines.append("| Diff non-empty | 🔶 needs review | 0 changed files |")
        else:
            lines.append("| Diff non-empty | ✅ passed | changes detected |")

        if gr.review_hard_block:
            lines.append("| Review hard block | ❌ failed | true |")
        else:
            lines.append("| Review hard block | ✅ passed | false |")

        if gr.review_recommendation == "reject":
            lines.append("| Review recommendation | ❌ failed | reject |")
        elif gr.review_recommendation == "revise":
            lines.append("| Review recommendation | 🔶 needs review | revise |")
        else:
            lines.append("| Review recommendation | ✅ passed | merge |")

        lines.append("")
        lines.append(f"**Final gate outcome:** {_status_word(task.status)}")
        if gr.reasons:
            lines.append("")
            lines.append("**Gate reasons:**")
            for r in gr.reasons:
                lines.append(f"- {r}")
    else:
        # Fallback: render from raw data.
        if agent_result is None:
            lines.append("| Agent exit code | ❌ failed | no agent result |")
        elif agent_result.exit_code != 0:
            lines.append(f"| Agent exit code | ❌ failed | exit_code={agent_result.exit_code} |")
        else:
            lines.append(f"| Agent exit code | ✅ passed | exit_code={agent_result.exit_code} |")

        non_skipped = [r for r in command_results if not r.skipped]
        if len(non_skipped) == 0:
            lines.append("| Validation commands ran | 🔶 needs review | 0 commands ran |")
        else:
            lines.append(f"| Validation commands ran | ✅ passed | {len(non_skipped)} commands ran |")

        failed_cmds = [r for r in non_skipped if not r.passed]
        if failed_cmds:
            lines.append(f"| Validation commands passed | ❌ failed | {len(failed_cmds)}/{len(non_skipped)} passed |")
        elif non_skipped:
            lines.append(f"| Validation commands passed | ✅ passed | {len(non_skipped)}/{len(non_skipped)} passed |")
        else:
            lines.append("| Validation commands passed | 🔶 needs review | n/a |")

        if len(diff.changed_files) == 0:
            lines.append("| Diff non-empty | 🔶 needs review | 0 changed files |")
        else:
            lines.append(f"| Diff non-empty | ✅ passed | {len(diff.changed_files)} changed files |")

        if review.hard_block:
            lines.append("| Review hard block | ❌ failed | true |")
        else:
            lines.append("| Review hard block | ✅ passed | false |")

        if review.recommendation == Recommendation.REJECT:
            lines.append("| Review recommendation | ❌ failed | reject |")
        elif review.recommendation == Recommendation.REVISE:
            lines.append("| Review recommendation | 🔶 needs review | revise |")
        else:
            lines.append("| Review recommendation | ✅ passed | merge |")

        lines.append("")
        lines.append(f"**Final gate outcome:** {_status_word(task.status)}")

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
            f"| `{r.command or '\u2014'}` | {r.exit_code} | {outcome} | {r.duration_seconds} |"
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
            lines.append(f"- attempt {attempt} -- prompt: `artifacts/{prompt}`")
        lines.append("")

    # Memory candidates section
    lines.append("## Memory candidates")
    lines.append("")
    can_promote = (
        task.status == TaskStatus.PASSED
        and review.recommendation == Recommendation.MERGE
        and not review.hard_block
    )
    if can_promote:
        lines.append("This task **may** become a memory candidate once approved.")
        lines.append("")
        lines.append("- All gates passed. Review the vault note and set `approved: true` to enable memory promotion.")
        lines.append("- See [memory-promotion-rules](vault/rules/memory-promotion-rules.md) for eligibility criteria.")
    else:
        lines.append("This task is **not** a memory candidate until the conditions below are resolved:")
        lines.append("")
        if task.status != TaskStatus.PASSED:
            lines.append(f"- **Status:** {_status_word(task.status)} -- only `PASSED` tasks qualify")
        if review.recommendation != Recommendation.MERGE:
            lines.append(f"- **Recommendation:** `{review.recommendation.value}` -- only `merge` qualifies")
        if review.hard_block:
            lines.append("- **Hard block:** active -- must be resolved before promotion")
        lines.append(f"- **Risk:** {review.risk.value} -- see [memory-promotion-rules](vault/rules/memory-promotion-rules.md)")
    lines.append("")

    lines.append("## Agent")
    lines.append("")
    if agent_result is not None:
        lines.append(f"- **Agent:** `{agent_result.agent_name}` (exit {agent_result.exit_code})")
        lines.append(f"- **Summary:** {agent_result.summary}")
        lines.append("- **stdout:** `artifacts/agent_stdout.txt`")
        lines.append("- **stderr:** `artifacts/agent_stderr.txt`")
    else:
        lines.append("_(no agent result -- run failed before agent execution)_")
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    lines.append("Run artifacts live under `data/runs/<task_id>/artifacts/`:")
    lines.append("")
    lines.append("- `agent_prompt.txt` -- what the agent was told")
    lines.append("- `agent_stdout.txt` / `agent_stderr.txt` -- agent output")
    lines.append("- `<cmd>_stdout.txt` / `<cmd>_stderr.txt` -- per-command output")
    lines.append("- `commands.json` -- command results table")
    lines.append("- `diff.patch` / `diff_stat.txt` -- the captured change")
    lines.append("- `review.json` -- this review, machine-readable")
    lines.append("")
    lines.append(
        "The event log (`data/runs/<task_id>/events.jsonl`) and the evidence "
        "manifest (`data/runs/<task_id>/evidence_manifest.json`) live in the "
        "run directory itself — the manifest holds content-addressed artifact "
        "hashes + the event chain head. The event log is the source of truth; "
        "this report is its human-readable projection."
    )
    if manifest_hash:
        lines.append("")
        lines.append(f"**Evidence manifest hash:** `{manifest_hash}`")
        lines.append("")
        lines.append(
            "This hash covers every artifact + the event chain head. "
            "Verify with `evidence_manifest.json` in the run directory."
        )
    lines.append("")

    # Event timeline — the report as a true projection of the event log.
    if events:
        lines.append("## Event timeline")
        lines.append("")
        lines.append(f"The complete event log ({len(events)} events, hash-chained):")
        lines.append("")
        lines.append("| # | event_id | type | timestamp | hash (first 12) |")
        lines.append("| --- | --- | --- | --- | --- |")
        for i, evt in enumerate(events):
            short_hash = evt.hash[:12] if evt.hash else "—"
            lines.append(
                f"| {i + 1} | `{evt.event_id}` | `{evt.type.value}` | {evt.timestamp} | `{short_hash}` |"
            )
        lines.append("")
        lines.append(
            "Each event's hash links to the previous event's hash, forming a "
            "tamper-evident chain. Verify with `verify_event_chain()`."
        )
        lines.append("")

    return "\n".join(lines)


def _status_word(status: TaskStatus) -> str:
    if status == TaskStatus.PASSED:
        return "\u2705 passed"
    if status == TaskStatus.FAILED:
        return "\u274c failed"
    if status == TaskStatus.NEEDS_REVIEW:
        return "\U0001f536 needs review"
    return status.value


def render_failure_report(
    *,
    task: Task,
    error: str,
    events: list[Event] | None = None,
    manifest_hash: str | None = None,
) -> str:
    """Minimal report for early failures (before diff/review exist).

    The spec rule is "a failed task produces an evidence report." For failures
    that happen before a diff is captured (dirty repo, worktree creation error,
    node crash before agent run), there's no diff or review to render — but
    there is still a task, an error, and an event log. This minimal report
    records what we know so the evidence trail is never empty. When ``events``
    is provided, an event timeline is included. When ``manifest_hash`` is
    provided, the evidence binding is recorded.
    """
    lines: list[str] = []
    lines.append(f"# Task report: {task.task_id}")
    lines.append("")
    lines.append(f"- **Status:** {_status_word(task.status)}")
    lines.append(f"- **Repo:** `{task.repo_name}` (`{task.base_branch}` -> `{task.task_branch}`)")
    lines.append(f"- **Base commit:** `{task.base_commit_sha or '(not recorded)'}`")
    lines.append(f"- **Request:** {task.user_request}")
    lines.append(f"- **Created:** {task.created_at}")
    lines.append("")
    lines.append("## Failure")
    lines.append("")
    lines.append("The task failed before producing a diff or review.")
    lines.append("")
    lines.append(f"**Error:** {error}")
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    lines.append("This task failed early — no diff, review, or command artifacts were produced.")
    lines.append("The event log (`events.jsonl`) is the source of truth for what happened.")
    lines.append("")
    if manifest_hash:
        lines.append(f"**Evidence manifest hash:** `{manifest_hash}`")
        lines.append("")
        lines.append(
            "This hash covers every artifact + the event chain head. "
            "Verify with `evidence_manifest.json` in the run directory."
        )
        lines.append("")

    # Event timeline — show what happened before the failure.
    if events:
        lines.append("## Event timeline")
        lines.append("")
        lines.append(f"The complete event log ({len(events)} events, hash-chained):")
        lines.append("")
        lines.append("| # | event_id | type | timestamp | hash (first 12) |")
        lines.append("| --- | --- | --- | --- | --- |")
        for i, evt in enumerate(events):
            short_hash = evt.hash[:12] if evt.hash else "—"
            lines.append(
                f"| {i + 1} | `{evt.event_id}` | `{evt.type.value}` | {evt.timestamp} | `{short_hash}` |"
            )
        lines.append("")

    return "\n".join(lines)
