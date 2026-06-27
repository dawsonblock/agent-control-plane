"""Mid-stream token/line analysis and safety gating.

Provides an async wrapper around agent execution for real-time safety,
attractor (strange-loop) detection, and dangerous-path flagging. This
module replaces the blocking ``subprocess.run`` path in
:class:`~acp.agents.cli_agent.CLIAgent` with an async streaming path
that feeds each line of agent stdout to a :class:`StreamSentinel`.

Design constraints (from the ACP architecture audit):

1. **Hash-chain invariant**: :class:`~acp.events.EventWriter` is
   thread-unsafe by design. During the stream, the sentinel is the sole
   writer (the graph node writes ``agent.started`` before and
   ``agent.finished`` after). All sentinel event writes are serialized
   via an ``asyncio.Lock`` to guarantee the hash chain stays valid.

2. **No fire-and-forget for blocking checks**: All hard-abort checks
   (secret, dangerous path, strange loop) run synchronously within
   :meth:`StreamSentinel.analyze_chunk`. v0.7.5 adds a non-blocking
   semantic anomaly check that runs asynchronously — it does NOT gate
   the stream, it flags anomalies for post-run review using a local
   sentence-transformers cross-encoder (no LLM round-trip needed).

3. **Strange-loop detection via token n-gram similarity**: The original
   prototype used ``md5(chunk)`` which only catches exact full-line
   repeats. Real attractors have token drift. This implementation uses
   3-gram Jaccard similarity against a rolling window of recent chunks.

4. **Process teardown**: ``terminate()`` → ``wait_for(wait(), timeout=2)``
   → ``kill()`` on timeout. No racy ``sleep(0.5)``.

5. **Subtask detection**: The sentinel detects ``ACP_SPAWN_SUBTASK:``
   lines mid-stream and records them internally, but does NOT emit
   ``task.subtask_spawned`` events — the existing post-run
   ``_parse_and_emit_subtasks`` in the graph node handles event emission
   to avoid duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from pathlib import Path
from typing import Any

from acp.config import StreamingSection
from acp.models import EventType
from acp.streaming.secret_stream_scanner import scan_stream

logger = logging.getLogger(__name__)

# Subtask spawn marker (same regex as acp.subtask, but we just detect presence).
_SPAWN_MARKER = "ACP_SPAWN_SUBTASK:"

# Token n-gram size for strange-loop similarity. 3-grams of whitespace-split
# tokens catch near-duplicate lines with small token drift (the real
# attractor pattern) without matching on single-word repeats.
_NGRAM_SIZE = 3


class StreamAbort(Exception):
    """Raised by :meth:`StreamSentinel.analyze_chunk` to signal an abort.

    Carries the abort ``reason`` (``"secret_detected"``, ``"strange_loop"``,
    or ``"dangerous_path"``) and a preview of the offending chunk. The
    caller (:func:`run_agent_streaming`) catches this, kills the agent
    process, and returns a non-zero exit code.
    """

    def __init__(self, reason: str, chunk_preview: str, detail: str = "") -> None:
        self.reason = reason
        self.chunk_preview = chunk_preview
        self.detail = detail
        super().__init__(f"Stream aborted: {reason} — {detail or chunk_preview[:80]}")


def _tokenize(text: str) -> list[str]:
    """Split text into whitespace-delimited tokens (lowercased)."""
    return text.lower().split()


def _ngrams(tokens: list[str], n: int = _NGRAM_SIZE) -> set[tuple[str, ...]]:
    """Return the set of n-grams from a token list.

    If the token list is shorter than n, returns a single tuple of all
    tokens (so very short lines still produce a comparable fingerprint).
    """
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _jaccard(a: set[tuple[str, ...]], b: set[tuple[str, ...]]) -> float:
    """Jaccard similarity between two n-gram sets (0.0–1.0)."""
    if not a and not b:
        return 1.0  # both empty → identical
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


class StreamSentinel:
    """Monitors an agent's output stream for anomalies and safety breaches.

    The sentinel is constructed once per agent run and fed each line of
    stdout via :meth:`analyze_chunk`. If a safety check triggers, the
    sentinel raises :class:`StreamAbort` to signal the caller to kill the
    agent process.

    All event writes are serialized via an ``asyncio.Lock`` to preserve
    the :class:`EventWriter` hash-chain invariant.
    """

    def __init__(
        self,
        task_id: str,
        events: Any | None = None,
        config: StreamingSection | None = None,
        *,
        custom_secret_regexes: list[tuple[str, re.Pattern[str]]] | None = None,
    ) -> None:
        self.task_id = task_id
        self.events = events
        self.config = config or StreamingSection()
        self.custom_secret_regexes = custom_secret_regexes

        # Strange-loop detection state.
        self._window: deque[set[tuple[str, ...]]] = deque(maxlen=self.config.strange_loop_window)
        self._repetition_score = 0.0
        self._chunk_count = 0

        # Subtask spawn requests detected mid-stream (recorded, not emitted).
        self.subtask_requests: list[str] = []

        # Dangerous-path patterns (compiled once).
        self._dangerous_patterns: list[re.Pattern[str]] = []
        for pattern in self.config.dangerous_path_patterns:
            try:
                self._dangerous_patterns.append(re.compile(pattern))
            except re.error:
                logger.warning("Invalid dangerous_path_pattern: %s", pattern)

        # Write lock for hash-chain safety.
        self._write_lock = asyncio.Lock()

        # Track whether we've been aborted to reject further chunks.
        self.is_aborted = False
        # The reason for the abort (set when is_aborted becomes True).
        # One of: "secret_detected", "strange_loop", "dangerous_path".
        self.abort_reason = ""

        # v0.7.5: Async semantic anomaly detection (non-blocking).
        # Anomalies are flagged for post-run review, not aborted mid-stream.
        self.semantic_anomalies: list[dict[str, Any]] = []
        self._semantic_model: Any = None
        self._semantic_embeddings: list[Any] = []
        self._semantic_task: asyncio.Task[None] | None = None
        if self.config.semantic_anomaly_detection:
            self._init_semantic_model()

    def _init_semantic_model(self) -> None:
        """Lazily load the sentence-transformers model for semantic anomaly detection."""
        try:
            from sentence_transformers import SentenceTransformer

            self._semantic_model = SentenceTransformer(
                self.config.semantic_anomaly_model,
                local_files_only=True,
            )
            logger.info(
                "semantic anomaly detection enabled: model=%s",
                self.config.semantic_anomaly_model,
            )
        except ImportError:
            logger.warning(
                "semantic_anomaly_detection enabled but sentence-transformers "
                "not installed — install with: uv sync --extra rag"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to load semantic model %s: %s — semantic anomaly detection disabled",
                self.config.semantic_anomaly_model,
                exc,
            )

    def _check_semantic_anomaly(self, chunk: str, chunk_index: int) -> None:
        """Check if a chunk is semantically anomalous (synchronous, fast).

        Uses cosine similarity between the chunk's embedding and the running
        average of previous embeddings. If similarity is below the threshold,
        the chunk is flagged as anomalous. This is a local operation (no LLM
        round-trip) and takes ~5-20ms on CPU with all-MiniLM-L6-v2.
        """
        if self._semantic_model is None:
            return

        try:
            import numpy as np  # type: ignore[import-not-found]

            embedding = self._semantic_model.encode(chunk, show_progress_bar=False)
            self._semantic_embeddings.append(embedding)

            # Need at least 3 chunks before we can compute meaningful averages.
            if len(self._semantic_embeddings) < 3:
                return

            # Compute running average of all previous embeddings.
            prev = np.array(self._semantic_embeddings[:-1])
            avg = prev.mean(axis=0)

            # Cosine similarity between current chunk and running average.
            norm_curr = np.linalg.norm(embedding)
            norm_avg = np.linalg.norm(avg)
            if norm_curr == 0 or norm_avg == 0:
                return
            similarity = float(np.dot(embedding, avg) / (norm_curr * norm_avg))

            if similarity < self.config.semantic_anomaly_threshold:
                self.semantic_anomalies.append(
                    {
                        "chunk_index": chunk_index,
                        "similarity": round(similarity, 4),
                        "threshold": self.config.semantic_anomaly_threshold,
                        "preview": chunk[:100],
                    }
                )
                logger.warning(
                    "semantic anomaly detected: chunk=%d similarity=%.4f "
                    "threshold=%.2f — flagged for post-run review",
                    chunk_index,
                    similarity,
                    self.config.semantic_anomaly_threshold,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("semantic anomaly check failed: %s", exc)

    async def analyze_chunk(self, chunk: str) -> None:
        """Run safety checks on a stream chunk.

        Raises :class:`StreamAbort` if a hard block is triggered (secret
        detected, strange loop, or dangerous path). The caller should
        catch this, kill the agent process, and record the abort.
        """
        if self.is_aborted or not chunk.strip():
            return

        self._chunk_count += 1
        chunk_index = self._chunk_count

        # 1. Kill-switch: secret detection.
        if self.config.secret_detection:
            findings = scan_stream(
                chunk,
                chunk_index=chunk_index,
                custom_regexes=self.custom_secret_regexes,
            )
            if findings:
                finding = findings[0]
                await self._emit_abort(
                    reason="secret_detected",
                    chunk_preview=chunk[:100],
                    detail=f"kind={finding.kind} snippet={finding.snippet}",
                )
                raise StreamAbort(
                    reason="secret_detected",
                    chunk_preview=chunk[:100],
                    detail=f"kind={finding.kind} snippet={finding.snippet}",
                )

        # 2. Dangerous-path detection.
        if self._dangerous_patterns:
            for pat in self._dangerous_patterns:
                if pat.search(chunk):
                    await self._emit_abort(
                        reason="dangerous_path",
                        chunk_preview=chunk[:100],
                        detail=f"pattern={pat.pattern}",
                    )
                    raise StreamAbort(
                        reason="dangerous_path",
                        chunk_preview=chunk[:100],
                        detail=f"pattern={pat.pattern}",
                    )

        # 3. Strange-loop / attractor detection.
        if self.config.strange_loop_detection:
            abort_info = self._check_strange_loop(chunk)
            if abort_info is not None:
                await self._emit_abort(
                    reason="strange_loop",
                    chunk_preview=chunk[:100],
                    detail=abort_info,
                )
                raise StreamAbort(
                    reason="strange_loop",
                    chunk_preview=chunk[:100],
                    detail=abort_info,
                )

        # 4. Subtask spawn detection (record only — post-run parser emits events).
        if _SPAWN_MARKER in chunk:
            request = chunk.split(_SPAWN_MARKER, 1)[1].strip()
            if request:
                self.subtask_requests.append(request)

        # 5. v0.7.5: Semantic anomaly detection (non-blocking, local model).
        # Uses a local sentence-transformers cross-encoder — no LLM round-trip.
        # Anomalies are flagged for post-run review, NOT aborted mid-stream.
        if self.config.semantic_anomaly_detection and self._semantic_model is not None:
            self._check_semantic_anomaly(chunk, chunk_index)

    def _check_strange_loop(self, chunk: str) -> str | None:
        """Update the strange-loop detector and return abort detail if triggered.

        Uses token 3-gram Jaccard similarity against a rolling window of
        recent chunks. If the current chunk is near-duplicate (similarity
        > ``strange_loop_similarity``) of any recent chunk, the repetition
        score increases. Each unique chunk decays the score. If the score
        exceeds ``strange_loop_threshold``, return the abort detail string.

        v0.7.4: Short chunks (< 4 tokens) are skipped — they produce
        trivially small n-gram sets that match too easily (e.g.
        ``assert x == 1`` in test boilerplate, ``logger.info(...)`` lines).
        The decay was also increased from 0.5 to 1.0 so that interspersed
        unique content resets the score faster, preventing false positives
        on agents that emit repetitive scaffolding mixed with unique output.

        Returns ``None`` if no abort is triggered, or a detail string
        describing the repetition score and max similarity.
        """
        tokens = _tokenize(chunk)
        if not tokens:
            return None

        # v0.7.4: Skip very short chunks — they produce n-gram sets too
        # small to be meaningful (e.g. "assert x == 1" → 2 trigrams). These
        # are common in code generation (boilerplate, test assertions,
        # logger calls) and would cause false-positive strange-loop aborts.
        if len(tokens) < 4:
            return None

        current_ngrams = _ngrams(tokens)

        # Check similarity against every chunk in the window.
        max_similarity = 0.0
        for prev_ngrams in self._window:
            sim = _jaccard(current_ngrams, prev_ngrams)
            if sim > max_similarity:
                max_similarity = sim

        if max_similarity >= self.config.strange_loop_similarity:
            # Near-duplicate found — increase score.
            self._repetition_score += 1.5
        else:
            # Unique content — decay the score.
            # v0.7.4: Increased decay from 0.5 to 1.0 so interspersed
            # unique content resets the score faster.
            self._repetition_score = max(0.0, self._repetition_score - 1.0)

        self._window.append(current_ngrams)

        if self._repetition_score > self.config.strange_loop_threshold:
            return (
                f"repetition_score={self._repetition_score:.1f} max_similarity={max_similarity:.2f}"
            )

        return None

    async def _emit_abort(self, reason: str, chunk_preview: str, detail: str) -> None:
        """Write a stream.aborted event to the hash-chained log.

        Serialized via ``asyncio.Lock`` to preserve the hash-chain invariant.
        The blocking ``EventWriter.write`` (which calls ``os.fsync``) is
        offloaded to a thread via ``asyncio.to_thread`` to avoid stalling
        the event loop.

        If no EventWriter is configured (e.g., in unit tests), this is a no-op.
        """
        self.is_aborted = True
        self.abort_reason = reason
        if self.events is None:
            return
        async with self._write_lock:
            # v0.7.4: EventWriter.write does open/fsync/close — blocking disk
            # I/O that would stall the asyncio event loop. Offload to a thread.
            await asyncio.to_thread(
                self.events.write,
                EventType.STREAM_ABORTED,
                {
                    "task_id": self.task_id,
                    "reason": reason,
                    "chunk_preview": chunk_preview,
                    "detail": detail,
                },
            )


async def run_agent_streaming(
    cmd: list[str],
    cwd: str,
    sentinel: StreamSentinel,
    *,
    timeout: int = 1800,
    use_shell: bool = False,
) -> tuple[int, str, str]:
    """Execute the agent process and feed stdout to the sentinel line-by-line.

    Replaces ``subprocess.run`` with ``asyncio.create_subprocess_exec`` for
    real-time stream analysis. If the sentinel raises :class:`StreamAbort`,
    the process is killed (terminate → wait 2s → kill) and the partial
    output is returned.

    v0.7.4: Output is spooled to temp files instead of accumulated in
    memory. This prevents OOM on long agent runs (e.g. OpenHands headless
    mode emits verbose JSONL tracing that can reach hundreds of MB).

    Args:
        cmd: The command list (or string if ``use_shell`` is True).
        cwd: Working directory for the process.
        sentinel: The :class:`StreamSentinel` to feed chunks to.
        timeout: Maximum execution time in seconds.
        use_shell: If True, run via ``shell=True`` (for shell metacharacters).

    Returns:
        A tuple of ``(exit_code, stdout, stderr)``.
    """
    import tempfile

    # v0.7.4: Spool to temp files to avoid OOM on long runs.
    stdout_file = tempfile.NamedTemporaryFile(mode="wb", suffix=".stdout", delete=False)
    stderr_file = tempfile.NamedTemporaryFile(mode="wb", suffix=".stderr", delete=False)
    stdout_path = Path(stdout_file.name)
    stderr_path = Path(stderr_file.name)

    def _read_spooled(path: Path) -> str:
        """Read spooled output from a temp file and clean up."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        if use_shell:
            # Shell mode: cmd is a single string, run via the shell.
            # This mirrors the shell=True path in CLIAgent for docker_sbx.
            process = await asyncio.create_subprocess_shell(
                cmd[0] if isinstance(cmd, list) else cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
    except Exception as exc:
        logger.error("Failed to spawn agent process: %s", exc)
        stdout_file.close()
        stderr_file.close()
        _read_spooled(stdout_path)
        _read_spooled(stderr_path)
        return 127, "", str(exc)

    # Close the file objects (we'll write via the path).
    stdout_file.close()
    stderr_file.close()

    async def _read_stderr() -> None:
        assert process.stderr is not None
        with open(stderr_path, "wb") as f:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                f.write(line)

    stderr_task = asyncio.create_task(_read_stderr())

    try:
        async with asyncio.timeout(timeout):
            assert process.stdout is not None
            with open(stdout_path, "wb") as f:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    f.write(line)
                    decoded = line.decode("utf-8", errors="replace")
                    await sentinel.analyze_chunk(decoded)

    except StreamAbort as exc:
        logger.warning("Stream aborted: %s", exc)
        await _kill_process(process)
        # The abort event was already emitted by _emit_abort in analyze_chunk.
        await stderr_task
        return 1, _read_spooled(stdout_path), _read_spooled(stderr_path)

    except TimeoutError:
        logger.warning("Agent timed out after %ds", timeout)
        await _kill_process(process)
        await stderr_task
        return 124, _read_spooled(stdout_path), _read_spooled(stderr_path)

    except Exception as exc:
        logger.error("Stream error: %s", exc)
        await _kill_process(process)
        await stderr_task
        return 127, _read_spooled(stdout_path), _read_spooled(stderr_path)

    # Normal completion — wait for the process to exit and stderr to drain.
    await stderr_task
    exit_code = await process.wait()
    return exit_code, _read_spooled(stdout_path), _read_spooled(stderr_path)


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a process: SIGTERM → wait 2s → SIGKILL.

    This is the safe teardown sequence. ``terminate()`` sends SIGTERM on
    Unix, allowing the agent to clean up. If it doesn't exit within 2
    seconds, ``kill()`` sends SIGKILL for an unconditional termination.
    """
    try:
        process.terminate()
    except ProcessLookupError:
        return  # already dead

    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
