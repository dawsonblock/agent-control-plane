"""Manual shell agent (M1).

The "agent" here is the human: we print the prompt and drop them into a
sub-shell inside the worktree. They make edits, type ``exit``, and the
control plane takes over (capture diff, run commands, review, report).

This is the M1 acceptance path: it proves the evidence loop works with a
real diff produced inside a real worktree, with no autonomous agent in the
loop. M2 swaps in a real CLI coding agent behind the same protocol.

A non-interactive mode is available for tests / CI: set ``ACP_TEST=1`` in the
environment and the agent skips the sub-shell, instead making a trivial
documented edit so the downstream diff/review/report path has something to
chew on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from acp.models import AgentResult


class ShellAgent:
    """Manual, human-in-the-loop agent."""

    name = "shell"

    def run(
        self,
        *,
        prompt_path: Path,
        worktree_path: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> AgentResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.txt"
        stderr_path = artifact_dir / "agent_stderr.txt"

        if os.environ.get("ACP_TEST") == "1":
            return self._run_test(prompt_path, worktree_path, stdout_path, stderr_path)

        # Interactive path: print the prompt, open a sub-shell in the worktree.
        prompt = prompt_path.read_text()
        print("\n" + "=" * 72)
        print("ACP manual shell agent — prompt follows")
        print("=" * 72)
        print(prompt)
        print("=" * 72)
        print(f"Opening a sub-shell in the worktree:\n  {worktree_path}")
        print("Make your edits, then type `exit` to hand control back to ACP.\n")

        shell = os.environ.get("SHELL", "/bin/bash")
        try:
            subprocess.run(
                [shell],
                cwd=str(worktree_path),
                check=False,
            )
            exit_code = 0
            err = ""
        except Exception as exc:  # noqa: BLE001
            exit_code = 1
            err = f"sub-shell failed: {exc}"

        stdout_path.write_text(
            "(interactive session output is not captured; see the worktree diff)\n"
        )
        stderr_path.write_text(err)
        return AgentResult(
            agent_name=self.name,
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary="Manual shell agent: human exited the sub-shell.",
        )

    # ------------------------------------------------------------------ #
    # Non-interactive path (tests only)
    # ------------------------------------------------------------------ #

    def _run_test(
        self,
        prompt_path: Path,
        worktree_path: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> AgentResult:
        """Make trivial edits so the evidence loop has a non-empty diff.

        Creates AGENT_NOTES.md (doc) and tests/test_agent_edit.py (test) so
        the diff reviewer doesn't flag ``tests_missing``. Stands in for a
        real agent in tests.
        """
        notes = worktree_path / "AGENT_NOTES.md"
        notes.write_text(
            f"# Agent notes\n\nEdited by ACP ShellAgent (test mode).\n\n"
            f"Prompt was read from: {prompt_path.name}\n"
        )
        test_dir = worktree_path / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "test_agent_edit.py").write_text(
            "def test_agent_edit():\n    assert True\n"
        )
        stdout_path.write_text(
            "ACP_TEST mode: ShellAgent created AGENT_NOTES.md + tests/test_agent_edit.py.\n"
        )
        stderr_path.write_text("")
        return AgentResult(
            agent_name=self.name,
            exit_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary="Manual shell agent (test mode): trivial edits made.",
        )
