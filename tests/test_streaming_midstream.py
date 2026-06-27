"""Tests for the mid-stream sentinel — real-time safety gating during agent execution.

Tests the three safety layers:
1. Kill-switch: secret detection in raw agent stdout
2. Strange-loop: near-duplicate output cycle detection
3. Dangerous-path: configurable regex pattern matching

Also tests:
- Hash-chain integrity: events written during streaming maintain a valid chain
- Process teardown: agent process is killed on abort
- Normal stream: benign output completes without false positives
- Subtask detection: ACP_SPAWN_SUBTASK lines are recorded mid-stream

Note: This codebase uses pytest-asyncio with ``asyncio_mode="auto"``, so
async test functions are automatically collected and run in an event loop.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

from acp.config import StreamingSection
from acp.events import EventWriter, verify_event_chain
from acp.models import Event, EventType
from acp.streaming.midstream import (
    StreamAbort,
    StreamSentinel,
    _jaccard,
    _ngrams,
    _tokenize,
    run_agent_streaming,
)
from acp.streaming.secret_stream_scanner import scan_stream

# --------------------------------------------------------------------------- #
# Stream secret scanner tests
# --------------------------------------------------------------------------- #


def test_scan_stream_detects_aws_key() -> None:
    """AWS access key in raw text (no diff prefix) is detected."""
    parts = ["AKIA", "IOSFODNN7EXAMPLE"]
    text = f"Setting up credentials: {''.join(parts)}"
    findings = scan_stream(text)
    kinds = {f.kind for f in findings}
    assert "aws_access_key" in kinds


def test_scan_stream_detects_github_pat() -> None:
    """GitHub PAT in raw text is detected."""
    parts = ["ghp", "_abcdef12345678901234567890123456789012"]
    text = f"export GITHUB_TOKEN={''.join(parts)}"
    findings = scan_stream(text)
    kinds = {f.kind for f in findings}
    assert "github_pat" in kinds


def test_scan_stream_detects_private_key_block() -> None:
    """Private key header in raw text is detected."""
    text = "-----BEGIN RSA PRIVATE KEY-----"
    findings = scan_stream(text)
    kinds = {f.kind for f in findings}
    assert "private_key_block" in kinds


def test_scan_stream_detects_jwt() -> None:
    """JWT token in raw text is detected."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNqP3kE4kUJ6Tq3yQw"
    text = f"Authorization: Bearer {jwt}"
    findings = scan_stream(text)
    kinds = {f.kind for f in findings}
    assert "jwt" in kinds


def test_scan_stream_detects_high_entropy_assignment() -> None:
    """High-entropy assignment in raw text is detected."""
    # Use a value that doesn't contain placeholder keywords (EXAMPLE, etc.).
    text = 'API_KEY = "xK9mQ2vR7nL4pW8jF3hT6bY5cZ1dG0sA9eU"'
    findings = scan_stream(text)
    kinds = {f.kind for f in findings}
    assert "high_entropy_assignment" in kinds


def test_scan_stream_ignores_placeholder() -> None:
    """Placeholder values are not flagged as secrets."""
    text = 'API_KEY = "YOUR_API_KEY_HERE_example123"'
    findings = scan_stream(text)
    assert len(findings) == 0, f"placeholder flagged: {findings}"


def test_scan_stream_ignores_bare_commit_sha() -> None:
    """A bare 40-hex string (commit SHA) in a log line is not flagged."""
    text = "Commit: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    findings = scan_stream(text)
    # The github_legacy_token pattern should NOT fire on a bare SHA in prose.
    kinds = {f.kind for f in findings}
    assert "github_legacy_token" not in kinds


def test_scan_stream_detects_custom_regex() -> None:
    """Custom regex patterns are checked in stream scanning."""
    custom = [("internal_key", re.compile(r"IAK-[A-Z0-9]{32}"))]
    text = "Using internal key: IAK-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    findings = scan_stream(text, custom_regexes=custom)
    kinds = {f.kind for f in findings}
    assert "internal_key" in kinds


# --------------------------------------------------------------------------- #
# N-gram / similarity utility tests
# --------------------------------------------------------------------------- #


def test_tokenize_basic() -> None:
    assert _tokenize("Hello World Foo") == ["hello", "world", "foo"]


def test_ngrams_basic() -> None:
    tokens = ["a", "b", "c", "d"]
    grams = _ngrams(tokens, n=3)
    assert ("a", "b", "c") in grams
    assert ("b", "c", "d") in grams
    assert len(grams) == 2


def test_ngrams_short_input() -> None:
    """Short token lists still produce a comparable fingerprint."""
    grams = _ngrams(["a", "b"], n=3)
    assert grams == {("a", "b")}


def test_jaccard_identical() -> None:
    a = {("x", "y"), ("y", "z")}
    assert _jaccard(a, a) == 1.0


def test_jaccard_disjoint() -> None:
    a = {("x", "y")}
    b = {("a", "b")}
    assert _jaccard(a, b) == 0.0


def test_jaccard_partial() -> None:
    a = {("x", "y"), ("y", "z")}
    b = {("x", "y"), ("a", "b")}
    # intersection=1, union=3 → 1/3
    assert abs(_jaccard(a, b) - 1 / 3) < 0.01


# --------------------------------------------------------------------------- #
# StreamSentinel — secret detection (kill-switch)
# --------------------------------------------------------------------------- #


def test_sentinel_aborts_on_secret() -> None:
    """Sentinel raises StreamAbort when a secret appears in the stream."""

    async def _run() -> None:
        sentinel = StreamSentinel(task_id="test-1", config=StreamingSection(enabled=True))
        await sentinel.analyze_chunk("Starting work on the feature\n")
        await sentinel.analyze_chunk("Editing src/main.py\n")
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret_chunk = f"export AWS_ACCESS_KEY_ID={''.join(parts)}\n"
        with pytest.raises(StreamAbort) as exc_info:
            await sentinel.analyze_chunk(secret_chunk)
        assert exc_info.value.reason == "secret_detected"
        assert sentinel.is_aborted

    asyncio.run(_run())


def test_sentinel_aborts_on_private_key() -> None:
    """Sentinel catches a private key block mid-stream."""

    async def _run() -> None:
        sentinel = StreamSentinel(task_id="test-2", config=StreamingSection(enabled=True))
        await sentinel.analyze_chunk("Writing config files\n")
        with pytest.raises(StreamAbort) as exc_info:
            await sentinel.analyze_chunk("-----BEGIN RSA PRIVATE KEY-----\n")
        assert exc_info.value.reason == "secret_detected"

    asyncio.run(_run())


def test_sentinel_no_false_positive_on_benign_output() -> None:
    """Normal agent output does not trigger an abort."""

    async def _run() -> None:
        sentinel = StreamSentinel(task_id="test-3", config=StreamingSection(enabled=True))
        chunks = [
            "I'll start by reading the existing code\n",
            "The main module needs a new function\n",
            "Let me add the function to handle the edge case\n",
            "Running the tests now\n",
            "All tests pass, exiting\n",
        ]
        for chunk in chunks:
            await sentinel.analyze_chunk(chunk)
        assert not sentinel.is_aborted

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# StreamSentinel — strange-loop detection
# --------------------------------------------------------------------------- #


def test_sentinel_aborts_on_strange_loop() -> None:
    """Repeated near-duplicate chunks trigger strange-loop abort."""

    async def _run() -> None:
        config = StreamingSection(
            enabled=True,
            strange_loop_threshold=5.0,
            strange_loop_similarity=0.6,
        )
        sentinel = StreamSentinel(task_id="test-4", config=config)
        repeated = "Calling tool foo with argument bar baz qux\n"
        with pytest.raises(StreamAbort) as exc_info:
            for _ in range(20):
                await sentinel.analyze_chunk(repeated)
        assert exc_info.value.reason == "strange_loop"
        assert sentinel.is_aborted

    asyncio.run(_run())


def test_sentinel_strange_loop_catches_near_duplicate() -> None:
    """Near-duplicate lines with small token drift are caught."""

    async def _run() -> None:
        config = StreamingSection(
            enabled=True,
            strange_loop_threshold=5.0,
            strange_loop_similarity=0.5,
        )
        sentinel = StreamSentinel(task_id="test-5", config=config)
        base = "Error: cannot find module foo in path bar baz\n"
        with pytest.raises(StreamAbort) as exc_info:
            for i in range(20):
                chunk = base.replace("foo", f"foo{i % 3}")
                await sentinel.analyze_chunk(chunk)
        assert exc_info.value.reason == "strange_loop"

    asyncio.run(_run())


def test_sentinel_no_strange_loop_on_progressive_output() -> None:
    """Progressive, non-repeating output does not trigger the loop detector."""

    async def _run() -> None:
        config = StreamingSection(
            enabled=True,
            strange_loop_threshold=5.0,
        )
        sentinel = StreamSentinel(task_id="test-6", config=config)
        for i in range(30):
            chunk = f"Processing file {i}.py with unique content number {i}\n"
            await sentinel.analyze_chunk(chunk)
        assert not sentinel.is_aborted

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# StreamSentinel — dangerous-path detection
# --------------------------------------------------------------------------- #


def test_sentinel_aborts_on_dangerous_path() -> None:
    """Configured dangerous-path patterns trigger an abort."""

    async def _run() -> None:
        config = StreamingSection(
            enabled=True,
            dangerous_path_patterns=[r"rm\s+-rf\s+/"],
        )
        sentinel = StreamSentinel(task_id="test-7", config=config)
        await sentinel.analyze_chunk("Working on the codebase\n")
        with pytest.raises(StreamAbort) as exc_info:
            await sentinel.analyze_chunk("Running: rm -rf / to clean up\n")
        assert exc_info.value.reason == "dangerous_path"

    asyncio.run(_run())


def test_sentinel_no_dangerous_path_without_config() -> None:
    """No dangerous-path patterns = no false positives."""

    async def _run() -> None:
        config = StreamingSection(enabled=True)
        sentinel = StreamSentinel(task_id="test-8", config=config)
        await sentinel.analyze_chunk("rm -rf /tmp/build_artifacts\n")
        assert not sentinel.is_aborted

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# StreamSentinel — subtask detection
# --------------------------------------------------------------------------- #


def test_sentinel_records_subtask_spawn() -> None:
    """ACP_SPAWN_SUBTASK lines are recorded mid-stream."""

    async def _run() -> None:
        sentinel = StreamSentinel(task_id="test-9", config=StreamingSection(enabled=True))
        await sentinel.analyze_chunk("Starting work\n")
        await sentinel.analyze_chunk("ACP_SPAWN_SUBTASK: Refactor the auth module\n")
        await sentinel.analyze_chunk("ACP_SPAWN_SUBTASK: Write tests for the API layer\n")
        assert len(sentinel.subtask_requests) == 2
        assert "Refactor the auth module" in sentinel.subtask_requests[0]
        assert "Write tests for the API layer" in sentinel.subtask_requests[1]
        assert not sentinel.is_aborted

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# StreamSentinel — event writing + hash-chain integrity
# --------------------------------------------------------------------------- #


def test_sentinel_writes_stream_aborted_event(tmp_path: Path) -> None:
    """The sentinel writes a stream.aborted event on abort, and the hash chain stays valid."""

    async def _run() -> None:
        run_dir = tmp_path / "run"
        writer = EventWriter(task_id="test-10", run_dir=run_dir)
        sentinel = StreamSentinel(
            task_id="test-10",
            events=writer,
            config=StreamingSection(enabled=True),
        )
        writer.write(EventType.AGENT_STARTED, {"agent": "test"})
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret_chunk = f"export AWS_KEY={''.join(parts)}\n"
        with pytest.raises(StreamAbort):
            await sentinel.analyze_chunk(secret_chunk)

        # Verify the event chain.
        events = []
        for line in (run_dir / "events.jsonl").open():
            if line.strip():
                events.append(Event.model_validate_json(line))
        assert len(events) == 2  # AGENT_STARTED + STREAM_ABORTED
        assert events[1].type == EventType.STREAM_ABORTED
        assert events[1].payload["reason"] == "secret_detected"
        assert verify_event_chain(events), "hash chain broken by sentinel event write"

    asyncio.run(_run())


def test_sentinel_no_event_writer_still_works() -> None:
    """Sentinel functions without an EventWriter (events=None)."""

    async def _run() -> None:
        sentinel = StreamSentinel(
            task_id="test-11",
            events=None,
            config=StreamingSection(enabled=True),
        )
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        with pytest.raises(StreamAbort):
            await sentinel.analyze_chunk(f"export AWS_KEY={''.join(parts)}\n")
        assert sentinel.is_aborted

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# run_agent_streaming — integration tests
# --------------------------------------------------------------------------- #


def test_run_agent_streaming_normal_completion() -> None:
    """A benign agent process completes normally via the streaming path."""

    async def _run() -> tuple[int, str, str]:
        sentinel = StreamSentinel(
            task_id="test-12",
            config=StreamingSection(enabled=True),
        )
        return await run_agent_streaming(
            cmd=[sys.executable, "-c", "print('hello world'); print('done')"],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )

    exit_code, stdout, _ = asyncio.run(_run())
    assert exit_code == 0
    assert "hello world" in stdout
    assert "done" in stdout


def test_run_agent_streaming_secret_abort() -> None:
    """The streaming path kills the agent when a secret appears in output."""

    async def _run() -> tuple[int, str, str]:
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        secret = "".join(parts)
        sentinel = StreamSentinel(
            task_id="test-13",
            config=StreamingSection(enabled=True),
        )
        return await run_agent_streaming(
            cmd=[
                sys.executable,
                "-c",
                f"print('working'); print('export AWS_KEY={secret}'); print('more output')",
            ],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )

    exit_code, stdout, _ = asyncio.run(_run())
    assert exit_code == 1  # aborted
    assert "working" in stdout  # partial output captured


def test_run_agent_streaming_strange_loop_abort() -> None:
    """The streaming path kills the agent on a strange loop."""

    async def _run() -> tuple[int, str, str]:
        config = StreamingSection(
            enabled=True,
            strange_loop_threshold=5.0,
            strange_loop_similarity=0.6,
        )
        sentinel = StreamSentinel(
            task_id="test-14",
            config=config,
        )
        return await run_agent_streaming(
            cmd=[
                sys.executable,
                "-c",
                "for _ in range(50): print('Error: cannot find module foo in path bar')",
            ],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )

    exit_code, _, _ = asyncio.run(_run())
    assert exit_code == 1  # aborted


def test_run_agent_streaming_timeout() -> None:
    """The streaming path handles timeout correctly."""

    async def _run() -> tuple[int, str, str]:
        sentinel = StreamSentinel(
            task_id="test-15",
            config=StreamingSection(enabled=True),
        )
        return await run_agent_streaming(
            cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=".",
            sentinel=sentinel,
            timeout=1,
        )

    exit_code, _, _ = asyncio.run(_run())
    assert exit_code == 124  # timeout exit code


def test_run_agent_streaming_exit_code_propagated() -> None:
    """A non-zero agent exit code (not abort) is propagated correctly."""

    async def _run() -> tuple[int, str, str]:
        sentinel = StreamSentinel(
            task_id="test-16",
            config=StreamingSection(enabled=True),
        )
        return await run_agent_streaming(
            cmd=[sys.executable, "-c", "import sys; sys.exit(42)"],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )

    exit_code, _, _ = asyncio.run(_run())
    assert exit_code == 42


# --------------------------------------------------------------------------- #
# Config validation tests
# --------------------------------------------------------------------------- #


def test_streaming_config_defaults() -> None:
    """Default streaming config is disabled with sensible thresholds."""
    config = StreamingSection()
    assert not config.enabled
    assert config.secret_detection
    assert config.strange_loop_detection
    assert config.strange_loop_threshold == 8.0
    assert config.strange_loop_window == 10
    assert config.strange_loop_similarity == 0.65


def test_streaming_config_validation_threshold() -> None:
    """Invalid threshold values are rejected."""
    with pytest.raises(ValueError, match="strange_loop_threshold"):
        StreamingSection(strange_loop_threshold=0)
    with pytest.raises(ValueError, match="strange_loop_threshold"):
        StreamingSection(strange_loop_threshold=101)


def test_streaming_config_validation_similarity() -> None:
    """Invalid similarity values are rejected."""
    with pytest.raises(ValueError, match="strange_loop_similarity"):
        StreamingSection(strange_loop_similarity=0.0)
    with pytest.raises(ValueError, match="strange_loop_similarity"):
        StreamingSection(strange_loop_similarity=1.5)


def test_streaming_config_validation_window() -> None:
    """Invalid window values are rejected."""
    with pytest.raises(ValueError, match="strange_loop_window"):
        StreamingSection(strange_loop_window=1)
    with pytest.raises(ValueError, match="strange_loop_window"):
        StreamingSection(strange_loop_window=101)


def test_streaming_config_validation_dangerous_patterns() -> None:
    """Invalid regex patterns in dangerous_path_patterns are rejected."""
    with pytest.raises(ValueError, match="dangerous_path_patterns"):
        StreamingSection(dangerous_path_patterns=["[invalid"])


def test_streaming_config_valid_dangerous_patterns() -> None:
    """Valid regex patterns are accepted."""
    config = StreamingSection(dangerous_path_patterns=[r"rm\s+-rf", r"policy\.json"])
    assert len(config.dangerous_path_patterns) == 2


# --------------------------------------------------------------------------- #
# Edge cases: spawn failure, stderr capture, disabled sub-checks, empty output
# --------------------------------------------------------------------------- #


def test_run_agent_streaming_spawn_failure() -> None:
    """A nonexistent command returns exit code 127 with an error message."""

    async def _run() -> tuple[int, str, str, StreamSentinel]:
        sentinel = StreamSentinel(
            task_id="test-spawn",
            config=StreamingSection(enabled=True),
        )
        code, out, err = await run_agent_streaming(
            cmd=["__nonexistent_binary_xyz__"],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )
        return code, out, err, sentinel

    exit_code, stdout, stderr, sentinel = asyncio.run(_run())
    assert exit_code == 127
    assert not sentinel.is_aborted
    # stderr should contain some error info (platform-dependent).
    assert len(stderr) > 0 or len(stdout) > 0


def test_run_agent_streaming_captures_stderr() -> None:
    """Stderr output is captured separately from stdout."""

    async def _run() -> tuple[int, str, str]:
        sentinel = StreamSentinel(
            task_id="test-stderr",
            config=StreamingSection(enabled=True),
        )
        return await run_agent_streaming(
            cmd=[sys.executable, "-c", "import sys; sys.stderr.write('error msg\\n')"],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )

    exit_code, stdout, stderr = asyncio.run(_run())
    assert exit_code == 0
    assert "error msg" in stderr
    assert "error msg" not in stdout


def test_sentinel_secret_detection_disabled() -> None:
    """When secret_detection is False, secrets in output do not trigger an abort."""

    async def _run() -> None:
        config = StreamingSection(enabled=True, secret_detection=False)
        sentinel = StreamSentinel(task_id="test-no-secret", config=config)
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        await sentinel.analyze_chunk(f"export AWS_KEY={''.join(parts)}\n")
        assert not sentinel.is_aborted

    asyncio.run(_run())


def test_sentinel_strange_loop_disabled() -> None:
    """When strange_loop_detection is False, repeated output does not trigger an abort."""

    async def _run() -> None:
        config = StreamingSection(
            enabled=True,
            strange_loop_detection=False,
            strange_loop_threshold=5.0,
        )
        sentinel = StreamSentinel(task_id="test-no-loop", config=config)
        repeated = "Error: cannot find module foo in path bar baz\n"
        for _ in range(20):
            await sentinel.analyze_chunk(repeated)
        assert not sentinel.is_aborted

    asyncio.run(_run())


def test_sentinel_empty_chunks() -> None:
    """Empty chunks and whitespace-only chunks do not trigger false positives."""

    async def _run() -> None:
        sentinel = StreamSentinel(
            task_id="test-empty",
            config=StreamingSection(enabled=True),
        )
        await sentinel.analyze_chunk("")
        await sentinel.analyze_chunk("\n")
        await sentinel.analyze_chunk("   \n")
        await sentinel.analyze_chunk("\t\t\n")
        assert not sentinel.is_aborted
        assert len(sentinel.subtask_requests) == 0

    asyncio.run(_run())


def test_sentinel_aborted_rejects_further_chunks() -> None:
    """After an abort, further chunks are silently dropped (no second abort)."""

    async def _run() -> None:
        sentinel = StreamSentinel(
            task_id="test-reject",
            config=StreamingSection(enabled=True),
        )
        parts = ["AKIA", "IOSFODNN7EXAMPLE"]
        with pytest.raises(StreamAbort):
            await sentinel.analyze_chunk(f"export AWS_KEY={''.join(parts)}\n")
        assert sentinel.is_aborted
        # Feeding more chunks should NOT raise — the sentinel is already aborted.
        await sentinel.analyze_chunk("more output\n")
        await sentinel.analyze_chunk("export ANOTHER_SECRET=ghp_1234567890\n")

    asyncio.run(_run())


def test_run_agent_streaming_empty_output() -> None:
    """A command that produces no output completes normally."""

    async def _run() -> tuple[int, str, str, StreamSentinel]:
        sentinel = StreamSentinel(
            task_id="test-empty-output",
            config=StreamingSection(enabled=True),
        )
        code, out, err = await run_agent_streaming(
            cmd=[sys.executable, "-c", "pass"],
            cwd=".",
            sentinel=sentinel,
            timeout=10,
        )
        return code, out, err, sentinel

    exit_code, stdout, stderr, sentinel = asyncio.run(_run())
    assert exit_code == 0
    assert stdout == ""
    assert stderr == ""
    assert not sentinel.is_aborted


def test_sentinel_subtask_detection_disabled_when_not_enabled() -> None:
    """Subtask detection works regardless of secret/loop config (it's always on)."""

    async def _run() -> None:
        config = StreamingSection(
            enabled=True,
            secret_detection=False,
            strange_loop_detection=False,
        )
        sentinel = StreamSentinel(task_id="test-subtask", config=config)
        await sentinel.analyze_chunk("ACP_SPAWN_SUBTASK: Write tests for API\n")
        assert len(sentinel.subtask_requests) == 1
        assert not sentinel.is_aborted

    asyncio.run(_run())


def test_sentinel_accumulates_subtask_requests() -> None:
    """Multiple subtask spawn lines are all recorded in order."""

    async def _run() -> None:
        sentinel = StreamSentinel(
            task_id="test-multi-subtask",
            config=StreamingSection(enabled=True),
        )
        for i in range(5):
            await sentinel.analyze_chunk(f"ACP_SPAWN_SUBTASK: Task number {i}\n")
        assert len(sentinel.subtask_requests) == 5
        for i, req in enumerate(sentinel.subtask_requests):
            assert f"Task number {i}" in req

    asyncio.run(_run())
