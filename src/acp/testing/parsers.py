"""Test-output parsers (stub — M4 repair loop will fill this in).

In M1 the report only needs a coarse pass/fail summary, which the runner
derives directly from exit codes. M4's repair loop will need structured
failure detail (which test, which assertion, which file) to construct a
repair prompt — that parsing lives here when it's built.

Keeping the module present now means M4 doesn't have to invent a new file;
it just implements the bodies.
"""

from __future__ import annotations

from pathlib import Path

from acp.models import CommandResult


def summarize(results: list[CommandResult]) -> dict[str, object]:
    """Coarse pass/fail summary for the report. M1-only.

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


# Reserved for M4: extract structured failures (test name, file, assertion)
# from a captured stdout file to feed a repair prompt.
def extract_failures(_stdout_path: Path) -> list[dict[str, object]]:  # pragma: no cover
    """Not implemented until M4 (repair loop)."""
    raise NotImplementedError("extract_failures is reserved for M4")
