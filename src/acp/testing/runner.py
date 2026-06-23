"""Command runner — executes the repo's configured install/lint/typecheck/test/build.

Every command runs with ``cwd = worktree_path``, so the agent's changes are
what gets exercised. Output is captured to per-command artifact files so the
report can cite exact stdout/stderr. Empty commands in config are skipped
(not failed) — a repo without a lint step shouldn't fail a run for its absence.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from acp.config import RepoConfig
from acp.models import CommandResult, EventType

if TYPE_CHECKING:
    from acp.events import EventWriter


def run_commands(
    *,
    repo_config: RepoConfig,
    worktree_path: Path,
    artifact_dir: Path,
    timeout_seconds: int = 300,
    event_writer: EventWriter | None = None,
) -> list[CommandResult]:
    """Run every non-empty command in config order; return one result each.

    Each command is run with a per-command timeout of ``timeout_seconds``.
    If a command exceeds the timeout it is killed (exit code 124).

    When ``event_writer`` is provided, writes ``command.started`` events
    before each command and ``command.finished`` events after each.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    results: list[CommandResult] = []

    for name, command in repo_config.commands.items():
        if not command.strip():
            results.append(_skipped(name, command, worktree_path, artifact_dir))
            continue
        if event_writer is not None:
            event_writer.write(EventType.COMMAND_STARTED, {"command": command, "name": name, "cwd": str(worktree_path)})
        result = _run_one(name, command, worktree_path, artifact_dir, timeout_seconds)
        if event_writer is not None:
            event_writer.write(
                EventType.COMMAND_FINISHED,
                {
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "skipped": result.skipped,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                    "stdout_path": str(result.stdout_path),
                    "stderr_path": str(result.stderr_path),
                },
            )
        results.append(result)

    # Persist the full table for the report.
    (artifact_dir / "commands.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in results], indent=2)
    )
    return results


def _run_one(
    name: str,
    command: str,
    cwd: Path,
    artifact_dir: Path,
    timeout_seconds: int = 300,
) -> CommandResult:
    """Run one command via the shell, capturing stdout/stderr to files.

    Timeout enforced via ``subprocess.run(timeout=...)``. Returns exit code
    124 (standard timeout exit code) if the command is killed.
    """
    stdout_path = artifact_dir / f"{name}_stdout.txt"
    stderr_path = artifact_dir / f"{name}_stderr.txt"

    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        exit_code = proc.returncode
        out, err = proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired:
        exit_code = 124
        timed_out = True
        out = ""
        err = f"acp: command timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001
        exit_code = 127
        timed_out = False
        out, err = "", f"acp: failed to spawn command: {exc}"

    duration = time.monotonic() - start
    stdout_path.write_text(out)
    stderr_path.write_text(err)

    return CommandResult(
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_seconds=round(duration, 3),
        timed_out=timed_out,
    )


def _skipped(name: str, command: str, cwd: Path, artifact_dir: Path) -> CommandResult:
    """A placeholder result for commands the config left empty."""
    stdout_path = artifact_dir / f"{name}_stdout.txt"
    stderr_path = artifact_dir / f"{name}_stderr.txt"
    stdout_path.write_text("")
    stderr_path.write_text(f"acp: '{name}' command is empty in config; skipped.\n")
    return CommandResult(
        command=command,
        cwd=cwd,
        exit_code=0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_seconds=0.0,
        skipped=True,
    )


def all_passed(results: list[CommandResult]) -> bool:
    """True iff no non-skipped command failed. (Skipped commands don't count.)"""
    return all(r.passed for r in results if not r.skipped)


def command_names_for_shell(command: str) -> list[str]:
    """Debug helper: show how shlex would split a command string."""
    return shlex.split(command)
