"""Mid-stream sentinel — real-time safety gating during agent execution.

When enabled via ``streaming.enabled`` in the repo config, the CLIAgent
switches from blocking ``subprocess.run`` to an async streaming path.
Each line of agent stdout is fed to a :class:`StreamSentinel` that runs
fast safety checks *before* the agent finishes:

1. **Kill-switch**: known credential patterns in the output stream →
   kill the process immediately, write a ``stream.aborted`` event.
2. **Strange-loop detection**: near-duplicate output cycles → kill the
   process, write a ``stream.aborted`` event.
3. **Dangerous-path detection**: configurable regex patterns → kill.

All event writes are serialized via an ``asyncio.Lock`` to preserve the
hash-chain invariant of the :class:`~acp.events.EventWriter`.
"""

from acp.streaming.midstream import StreamAbort, StreamSentinel, run_agent_streaming
from acp.streaming.secret_stream_scanner import StreamSecretFinding, scan_stream

__all__ = [
    "StreamAbort",
    "StreamSentinel",
    "run_agent_streaming",
    "StreamSecretFinding",
    "scan_stream",
]
