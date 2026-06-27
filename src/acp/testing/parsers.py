"""Test-output parsing for the repair loop (M4).

M1's ``summarize`` gives a coarse pass/fail. M4's ``extract_failures`` lifts
the structured detail a repair prompt needs: each failed command, its exit
code, and the captured stdout/stderr (truncated so the prompt fits). The
repair agent uses this to fix the failing tests rather than guess blindly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from acp.models import CommandResult

# Cap each captured output stream fed to a repair prompt — agents don't need
# a full test log, just the failing signal. 8 KiB is plenty for most suites.
_MAX_STREAM_BYTES = 8 * 1024


class CommandSummary(TypedDict):
    """Coarse pass/fail summary returned by :func:`summarize`."""

    passed: bool
    total: int
    failed: list[str]
    skipped: list[str]


def summarize(results: list[CommandResult]) -> CommandSummary:
    """Coarse pass/fail summary for the report.

    Returns ``{passed: bool, total: int, failed: list[name], skipped: list[name]}``.
    """
    ran = [r for r in results if not r.skipped]
    failed = [r.command for r in ran if not r.passed]
    skipped = [r.command for r in results if r.skipped]
    return {
        "passed": len(failed) == 0,
        "total": len(ran),
        "failed": failed,
        "skipped": skipped,
    }


def extract_failures(
    results: list[CommandResult], *, max_stream_bytes: int = _MAX_STREAM_BYTES
) -> list[dict[str, object]]:
    """Return one record per failed (non-skipped) command.

    Each record: ``{command, exit_code, stdout, stderr}`` where the streams
    are truncated to ``max_stream_bytes`` and read from the captured files
    (missing files yield an empty string rather than raising — the repair
    loop must be robust to partial captures).
    """
    failures: list[dict[str, object]] = []
    for r in results:
        if r.skipped or r.passed:
            continue
        failures.append(
            {
                "command": r.command,
                "exit_code": r.exit_code,
                "stdout": _read_truncated(r.stdout_path, max_stream_bytes),
                "stderr": _read_truncated(r.stderr_path, max_stream_bytes),
            }
        )
    return failures


def _read_truncated(path: Path, limit: int) -> str:
    """Read up to ``limit`` bytes from a path; '' if unreadable."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    if len(text.encode(errors="replace")) <= limit:
        return text
    # Truncate by encoded length, then re-decode to avoid splitting bytes.
    truncated = text.encode(errors="replace")[:limit].decode(errors="replace")
    return truncated + "\n...[truncated]"
