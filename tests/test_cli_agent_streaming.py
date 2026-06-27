"""End-to-end tests for CLIAgent streaming — the full run() path with mid-stream sentinel.

These tests exercise the complete CLIAgent.run() code path with
``streaming.enabled=True``, verifying that:

1. Normal commands complete successfully via the streaming path
2. Secret detection kills the agent and sets aborted_by_sentinel
3. Strange-loop detection kills the agent and sets aborted_by_sentinel
4. Dangerous-path detection kills the agent and sets aborted_by_sentinel
5. The stdout/stderr artifacts are written correctly
6. The AgentResult fields are populated correctly
7. The event_writer receives stream.aborted events with valid hash chains
8. Streaming disabled falls back to the normal subprocess.run path

Unlike test_streaming_midstream.py (which tests the sentinel in isolation),
these tests go through CLIAgent.run() → _run_streaming() → asyncio.run()
→ run_agent_streaming() → StreamSentinel — the full integration path.

Note: Commands are written to script files to avoid shell metacharacters
(`;` in inline Python) which are refused in worktree mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

from acp.agents.cli_agent import CLIAgent
from acp.config import (
    AgentSection,
    CommandsSection,
    ExecutorSection,
    RepoConfig,
    RepoSection,
    ReviewSection,
    StreamingSection,
)
from acp.events import EventWriter, verify_event_chain
from acp.models import Event, EventType


def _streaming_cfg(
    repo_path: Path,
    command_template: str,
    *,
    streaming: StreamingSection | None = None,
    review: ReviewSection | None = None,
) -> RepoConfig:
    """Build a RepoConfig with streaming enabled and a custom command template."""
    return RepoConfig(
        repo=RepoSection(name="test", path=repo_path, default_branch="main"),
        agent=AgentSection(
            default="custom",
            command_template=command_template,
        ),
        commands=CommandsSection(),
        review=review or ReviewSection(),
        executor=ExecutorSection(backend="worktree"),
        streaming=streaming or StreamingSection(enabled=True),
    )


def _write_script(tmp_path: Path, name: str, code: str) -> Path:
    """Write a Python script to a temp file and return its path.

    This avoids shell metacharacters (semicolons in inline -c commands)
    which are refused in worktree mode.
    """
    script = tmp_path / name
    script.write_text(code)
    return script


class TestCLIAgentStreamingNormal:
    """Normal commands complete successfully via the streaming path."""

    async def test_simple_echo(self, tmp_path: Path) -> None:
        """A simple echo command completes via streaming."""
        script = _write_script(tmp_path, "echo.py", "print('hello streaming')\n")
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert not result.aborted_by_sentinel
        assert "hello streaming" in (tmp_path / "artifacts" / "agent_stdout.txt").read_text()

    async def test_multi_line_output(self, tmp_path: Path) -> None:
        """Multi-line output is captured completely via streaming."""
        script = _write_script(
            tmp_path,
            "multi.py",
            "print('line1')\nprint('line2')\nprint('line3')\n",
        )
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        stdout = (tmp_path / "artifacts" / "agent_stdout.txt").read_text()
        assert "line1" in stdout
        assert "line2" in stdout
        assert "line3" in stdout

    async def test_nonzero_exit_propagated(self, tmp_path: Path) -> None:
        """A non-zero exit code is propagated through the streaming path."""
        script = _write_script(tmp_path, "exit42.py", "import sys\nsys.exit(42)\n")
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.exit_code == 42
        assert not result.aborted_by_sentinel


class TestCLIAgentStreamingSecretAbort:
    """Secret detection kills the agent via the streaming path."""

    async def test_aws_key_aborts(self, tmp_path: Path) -> None:
        """An AWS key in agent output triggers a sentinel abort."""
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret = "".join(parts)
        script = _write_script(
            tmp_path,
            "leak.py",
            f"print('working')\nprint('export AWS_KEY={secret}')\n",
        )
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.exit_code == 1
        assert result.aborted_by_sentinel
        assert result.sentinel_abort_reason == "secret_detected"
        # Partial output should be captured.
        stdout = (tmp_path / "artifacts" / "agent_stdout.txt").read_text()
        assert "working" in stdout

    async def test_private_key_aborts(self, tmp_path: Path) -> None:
        """A private key block in agent output triggers a sentinel abort."""
        script = _write_script(tmp_path, "pkey.py", "print('-----BEGIN RSA PRIVATE KEY-----')\n")
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.aborted_by_sentinel
        assert result.sentinel_abort_reason == "secret_detected"

    async def test_benign_output_not_aborted(self, tmp_path: Path) -> None:
        """Output without secrets does not trigger an abort."""
        script = _write_script(
            tmp_path,
            "benign.py",
            "print('editing src/main.py')\nprint('running tests')\n",
        )
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert not result.aborted_by_sentinel
        assert result.exit_code == 0


class TestCLIAgentStreamingStrangeLoopAbort:
    """Strange-loop detection kills the agent via the streaming path."""

    async def test_repeated_output_aborts(self, tmp_path: Path) -> None:
        """Repeated identical output triggers a strange-loop abort."""
        script = _write_script(
            tmp_path,
            "loop.py",
            "for _ in range(50):\n    print('Error: cannot find module foo in path bar')\n",
        )
        cfg = _streaming_cfg(
            tmp_path,
            f"{sys.executable} {script}",
            streaming=StreamingSection(
                enabled=True,
                strange_loop_threshold=5.0,
                strange_loop_similarity=0.6,
            ),
        )
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.aborted_by_sentinel
        assert result.sentinel_abort_reason == "strange_loop"

    async def test_progressive_output_not_aborted(self, tmp_path: Path) -> None:
        """Progressive, non-repeating output does not trigger a loop abort."""
        script = _write_script(
            tmp_path,
            "progress.py",
            "for i in range(20):\n    print(f'Processing file {i}.py with unique content')\n",
        )
        cfg = _streaming_cfg(
            tmp_path,
            f"{sys.executable} {script}",
            streaming=StreamingSection(
                enabled=True,
                strange_loop_threshold=5.0,
            ),
        )
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert not result.aborted_by_sentinel
        assert result.exit_code == 0


class TestCLIAgentStreamingDangerousPathAbort:
    """Dangerous-path detection kills the agent via the streaming path."""

    async def test_dangerous_path_aborts(self, tmp_path: Path) -> None:
        """A configured dangerous-path pattern triggers an abort."""
        script = _write_script(tmp_path, "danger.py", "print('Running: rm -rf / to clean up')\n")
        cfg = _streaming_cfg(
            tmp_path,
            f"{sys.executable} {script}",
            streaming=StreamingSection(
                enabled=True,
                dangerous_path_patterns=[r"rm\s+-rf\s+/"],
            ),
        )
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.aborted_by_sentinel
        assert result.sentinel_abort_reason == "dangerous_path"


class TestCLIAgentStreamingEventWriter:
    """The event_writer receives stream.aborted events with valid hash chains."""

    async def test_stream_aborted_event_written(self, tmp_path: Path) -> None:
        """A stream.aborted event is written to the event log on abort."""
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret = "".join(parts)
        script = _write_script(tmp_path, "leak.py", f"print('export AWS_KEY={secret}')\n")
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")

        run_dir = tmp_path / "run"
        writer = EventWriter(task_id="test-stream-1", run_dir=run_dir)

        agent = CLIAgent(cfg)
        agent.event_writer = writer
        agent.task_id = "test-stream-1"

        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )

        assert result.aborted_by_sentinel

        # Read the event log and verify the stream.aborted event.
        events = []
        for line in (run_dir / "events.jsonl").open():
            if line.strip():
                events.append(Event.model_validate_json(line))

        # Should have at least the stream.aborted event.
        abort_events = [e for e in events if e.type == EventType.STREAM_ABORTED]
        assert len(abort_events) == 1
        assert abort_events[0].payload["reason"] == "secret_detected"
        assert abort_events[0].payload["task_id"] == "test-stream-1"

        # Verify the hash chain is intact.
        assert verify_event_chain(events), "hash chain broken by streaming event writes"

    async def test_no_event_writer_still_works(self, tmp_path: Path) -> None:
        """CLIAgent streaming works without an event_writer (no events written)."""
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret = "".join(parts)
        script = _write_script(tmp_path, "leak.py", f"print('export AWS_KEY={secret}')\n")
        cfg = _streaming_cfg(tmp_path, f"{sys.executable} {script}")

        agent = CLIAgent(cfg)
        # Don't set event_writer — it defaults to None.

        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )

        assert result.aborted_by_sentinel
        assert result.sentinel_abort_reason == "secret_detected"


class TestCLIAgentStreamingDisabled:
    """When streaming is disabled, the normal subprocess.run path is used."""

    async def test_disabled_uses_normal_path(self, tmp_path: Path) -> None:
        """Streaming disabled falls back to subprocess.run."""
        script = _write_script(tmp_path, "normal.py", "print('normal path')\n")
        cfg = _streaming_cfg(
            tmp_path,
            f"{sys.executable} {script}",
            streaming=StreamingSection(enabled=False),
        )
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert not result.aborted_by_sentinel
        # The summary should NOT say "streaming" — it's the normal path.
        assert "streaming" not in result.summary.lower()

    async def test_disabled_does_not_detect_secrets(self, tmp_path: Path) -> None:
        """When streaming is disabled, secrets in output are NOT caught mid-stream.

        The post-run review_diff scanner will still catch them, but the
        mid-stream kill-switch is inactive.
        """
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret = "".join(parts)
        script = _write_script(tmp_path, "leak.py", f"print('export AWS_KEY={secret}')\n")
        cfg = _streaming_cfg(
            tmp_path,
            f"{sys.executable} {script}",
            streaming=StreamingSection(enabled=False),
        )
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        # The agent completes normally — no mid-stream abort.
        assert result.exit_code == 0
        assert not result.aborted_by_sentinel


class TestCLIAgentStreamingCustomRegex:
    """Custom secret regexes from the review config are used by the sentinel."""

    async def test_custom_regex_aborts(self, tmp_path: Path) -> None:
        """A custom secret regex from review config triggers a sentinel abort."""
        script = _write_script(
            tmp_path,
            "custom.py",
            "print('Using key: IAK-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345')\n",
        )
        cfg = _streaming_cfg(
            tmp_path,
            f"{sys.executable} {script}",
            review=ReviewSection(
                custom_secret_regexes=[{"name": "internal_key", "pattern": r"IAK-[A-Z0-9]{32}"}],
            ),
        )
        agent = CLIAgent(cfg)
        result = await agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.aborted_by_sentinel
        assert result.sentinel_abort_reason == "secret_detected"
