"""Generic CLI agent adapter (M2).

Runs any external coding agent (Claude Code, Codex, OpenCode, a custom
shell command) by substituting a ``command_template`` from the repo config
and shelling out. The adapter is intentionally agnostic about *which* agent
it's calling — it just substitutes placeholders, runs the command in the
worktree, and captures output. Swapping agents is a config change only;
the workflow never changes.

**Security note:** By default, the command is split with ``shlex.split()``
and run with ``shell=False`` — no shell interpretation. If the template
contains shell metacharacters (``|``, ``>``, ``<``, ``&``, ``;``, ``$``,
backticks), ``shell=True`` is used as a fallback, but **only** when the
executor backend is ``docker_sbx`` (sandboxed). In ``worktree`` mode, shell
metacharacters are refused to prevent RCE on the host.

Supported placeholders in ``command_template``:

    {prompt_path}        → path to the agent prompt file
    {worktree_path}      → the isolated worktree the agent edits in
    {context_bundle_path}→ the M6 context bundle (empty/"-" until Haystack lands)
    {artifact_dir}       → where to write agent stdout/stderr
    {timeout}            → the configured timeout in seconds
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from acp.config import RepoConfig
from acp.errors import AgentConfigError
from acp.models import AgentResult

# Shell metacharacters that require shell=True. If any of these appear in
# the rendered command, we either use shell=True (in docker_sbx mode) or
# refuse to run (in worktree mode).
_SHELL_METACHARS = set("|<>;&$\n`")


def _needs_shell(command: str) -> bool:
    """Return True if the command contains shell metacharacters."""
    return any(ch in _SHELL_METACHARS for ch in command)


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
        self._backend = config.executor.backend
        self._allow_shell = config.agent.allow_shell
        # v0.7.3: Optional EventWriter for mid-stream sentinel event writes.
        # Set by run_agent_node when streaming is enabled. When None, the
        # sentinel still runs safety checks but doesn't write events.
        self.event_writer: Any = None
        # v0.7.3: Task ID for the current run (set by run_agent_node).
        # Used by the sentinel in stream.aborted event payloads.
        self.task_id: str = ""

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

        # Determine whether we need shell=True for shell metacharacters.
        use_shell = False
        if _needs_shell(command):
            if self._backend == "docker_sbx":
                # Sandboxed — shell features are safe inside the microVM.
                use_shell = True
            elif self._allow_shell:
                # Operator explicitly opted in — trusted config.
                use_shell = True
            else:
                # Worktree mode — running on the host. Refuse shell
                # metacharacters to prevent RCE via manipulated config.
                raise AgentConfigError(
                    f"agent.command_template contains shell metacharacters "
                    f"(|, >, <, &, ;, $, backticks) but executor.backend is "
                    f"'worktree' — these are only allowed in 'docker_sbx' mode "
                    f"or when agent.allow_shell=True. Either switch to "
                    f"docker_sbx, set allow_shell: true in the repo config, "
                    f"or simplify the command template.\n"
                    f"Template: {command}"
                )

        argv = command if use_shell else shlex.split(command)

        # v0.7.3: Mid-stream sentinel — when streaming is enabled, use the
        # async streaming path instead of blocking subprocess.run. The
        # sentinel feeds each line of stdout to real-time safety checks
        # (secret detection, strange-loop detection, dangerous paths).
        if self.config.streaming.enabled:
            return self._run_streaming(
                argv=argv,
                cwd=str(worktree_path),
                use_shell=use_shell,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout_seconds=timeout_seconds,
                command=command,
            )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(worktree_path),
                shell=use_shell,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124  # standard timeout exit code
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
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
            summary=(f"CLI agent ran: {command} (exit {exit_code}, {duration:.2f}s)"),
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

    def _run_streaming(
        self,
        *,
        argv: list[str] | str,
        cwd: str,
        use_shell: bool,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
        command: str,
    ) -> AgentResult:
        """Run the agent via the async streaming path with mid-stream safety checks.

        This is the v0.7.3 streaming execution path. Instead of blocking on
        ``subprocess.run``, it uses ``asyncio.run`` to drive
        :func:`~acp.streaming.midstream.run_agent_streaming`, which feeds
        each line of agent stdout to a
        :class:`~acp.streaming.midstream.StreamSentinel` for real-time
        safety gating.

        The sentinel can kill the agent mid-stream if a secret leak,
        strange-loop, or dangerous-path pattern is detected. The abort is
        recorded as a ``stream.aborted`` event in the hash-chained event log.

        This method is safe to call from the sync graph node because the
        graph is invoked via ``wf.invoke()`` (sync) — there is no running
        event loop in the calling thread, so ``asyncio.run`` is valid.
        """
        from acp.streaming.midstream import StreamSentinel, run_agent_streaming

        # Build custom secret regexes from the review config.
        custom_regexes: list[tuple[str, Any]] = []
        for entry in self.config.review.custom_secret_regexes:
            import re

            custom_regexes.append((entry["name"], re.compile(entry["pattern"])))

        sentinel = StreamSentinel(
            task_id=self.task_id,
            events=self.event_writer,
            config=self.config.streaming,
            custom_secret_regexes=custom_regexes or None,
        )

        start = time.monotonic()
        exit_code, out, err = asyncio.run(
            run_agent_streaming(
                cmd=argv if isinstance(argv, list) else [argv],
                cwd=cwd,
                sentinel=sentinel,
                timeout=timeout_seconds,
                use_shell=use_shell,
            )
        )
        duration = time.monotonic() - start

        stdout_path.write_text(out)
        stderr_path.write_text(err)

        # If the sentinel aborted, note it in the summary and result fields.
        summary = f"CLI agent ran (streaming): {command} (exit {exit_code}, {duration:.2f}s)"
        if sentinel.is_aborted:
            summary += f" [ABORTED by sentinel: {sentinel.abort_reason}]"

        return AgentResult(
            agent_name=self.name,
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            summary=summary,
            aborted_by_sentinel=sentinel.is_aborted,
            sentinel_abort_reason=sentinel.abort_reason,
        )
