"""Generic CLI agent adapter (M2).

Runs any external coding agent (Claude Code, Codex, OpenCode, a custom
shell command) by substituting a ``command_template`` from the repo config
and shelling out. The adapter is intentionally agnostic about *which* agent
it's calling — it just substitutes placeholders, runs the command in the
worktree, and captures output. Swapping agents is a config change only;
the workflow never changes.

Supported placeholders in ``command_template``:

    {prompt_path}        → path to the agent prompt file
    {worktree_path}      → the isolated worktree the agent edits in
    {context_bundle_path}→ the M6 context bundle (empty/"-" until Haystack lands)
    {artifact_dir}       → where to write agent stdout/stderr
    {timeout}            → the configured timeout in seconds
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from acp.config import RepoConfig
from acp.errors import AgentConfigError
from acp.models import AgentResult


class CLIAgent:
    """Runs an external coding agent via a configured command template."""

    name = "custom"

    def __init__(self, config: RepoConfig) -> None:
        self.config = config
        template = config.agent.command_template.strip()
        if not template:
            raise AgentConfigError(
                "agent.default='custom' requires a non-empty "
                "agent.command_template in the repo config."
            )
        self.template = template

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

        command = self._render(
            prompt_path=prompt_path,
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
        )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=str(worktree_path),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124  # standard timeout exit code
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            err = f"acp: agent timed out after {timeout_seconds}s\n{err}"
        except Exception as exc:  # noqa: BLE001
            exit_code = 127
            out, err = "", f"acp: failed to spawn agent: {exc}"

        duration = time.monotonic() - start
        stdout_path.write_text(out)
        stderr_path.write_text(err)

        return AgentResult(
            agent_name=self.name,
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary=(
                f"CLI agent ran: {command} "
                f"(exit {exit_code}, {duration:.2f}s)"
            ),
        )

    def _render(
        self,
        *,
        prompt_path: Path,
        worktree_path: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> str:
        """Substitute placeholders. Raises AgentConfigError if any are left."""
        try:
            rendered = self.template.format(
                prompt_path=str(prompt_path),
                worktree_path=str(worktree_path),
                context_bundle_path="-",  # M6 will supply a real bundle
                artifact_dir=str(artifact_dir),
                timeout=timeout_seconds,
            )
        except KeyError as exc:
            raise AgentConfigError(
                f"command_template references unknown placeholder {exc}. "
                f"Supported: {{prompt_path}} {{worktree_path}} "
                f"{{context_bundle_path}} {{artifact_dir}} {{timeout}}"
            ) from exc
        return rendered

    @staticmethod
    def preview_command(template: str, **kwargs: object) -> list[str]:
        """Debug helper: show how a template would split into argv."""
        return shlex.split(template.format(**kwargs))
