"""Error-handling tests for src/acp/federation/transport.py.

Focuses on the :class:`StdioTransport` (JSON-RPC 2.0 over a subprocess
stdin/stdout). The subprocess is mocked so no real MCP server is required.
Each test exercises one failure mode of the transport and asserts that it
surfaces as an :class:`MCPError` with a descriptive message.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from acp.federation.transport import (
    MCPError,
    SSETransport,
    StdioTransport,
    create_transport,
)

# --------------------------------------------------------------------------- #
# Helpers — build a fake Popen with controllable stdin/stdout/stderr
# --------------------------------------------------------------------------- #


def _fake_popen(
    *,
    stdout_lines: list[str] | None = None,
    poll_value: int | None = None,
    write_raises: Exception | None = None,
) -> MagicMock:
    """Construct a MagicMock resembling subprocess.Popen.

    stdout_lines: lines the fake stdout.readline() will yield in order.
    poll_value: return value of proc.poll() (None => still running).
    write_raises: if set, proc.stdin.write() raises this exception.
    """
    proc = MagicMock()
    proc.poll.return_value = poll_value
    proc.stdin = MagicMock()
    proc.stderr = MagicMock()

    stdout = MagicMock()
    if stdout_lines is not None:
        # readline pops the first remaining line each call.
        remaining = list(stdout_lines)

        def _readline():
            if remaining:
                return remaining.pop(0)
                return ""

        stdout.readline.side_effect = _readline
    else:
        stdout.readline.return_value = ""
    proc.stdout = stdout

    if write_raises is not None:
        proc.stdin.write.side_effect = write_raises
        proc.stdin.flush.side_effect = write_raises

    return proc


# --------------------------------------------------------------------------- #
# start() — command not found / connection refused
# --------------------------------------------------------------------------- #


def test_transport_command_not_found():
    """FileNotFoundError from Popen is wrapped as MCPError on start()."""
    transport = StdioTransport(
        command=["this-command-does-not-exist-anywhere-xyz"],
        server_name="missing",
    )
    with patch(
        "acp.federation.transport.subprocess.Popen",
        side_effect=FileNotFoundError("no such file"),
    ):
        with pytest.raises(MCPError, match="command not found"):
            transport.start()
    assert transport.is_connected is False


def test_transport_start_is_idempotent():
    """Calling start() twice does not spawn a second process."""
    transport = StdioTransport(command=["echo"], server_name="s")
    fake = _fake_popen(stdout_lines=["{}"])
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake) as mock_popen:
        transport.start()
        transport.start()
        mock_popen.assert_called_once()
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — not running
# --------------------------------------------------------------------------- #


def test_transport_send_when_not_started():
    """send_request before start() raises MCPError mentioning 'not running'."""
    transport = StdioTransport(command=["echo"], server_name="s")
    with pytest.raises(MCPError, match="not running"):
        transport.send_request({"method": "ping"})


def test_transport_send_when_process_exited():
    """send_request after the process exited raises MCPError."""
    transport = StdioTransport(command=["echo"], server_name="s")
    fake = _fake_popen(poll_value=0)  # process already exited
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    with pytest.raises(MCPError, match="not running"):
        transport.send_request({"method": "ping"})
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — write failure (connection refused / broken pipe)
# --------------------------------------------------------------------------- #


def test_transport_connection_refused_on_write():
    """A BrokenPipeError during write is wrapped as MCPError."""
    transport = StdioTransport(command=["echo"], server_name="s")
    fake = _fake_popen(write_raises=BrokenPipeError("broken pipe"))
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    with pytest.raises(MCPError, match="closed connection during write"):
        transport.send_request({"method": "ping"})
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — timeout
# --------------------------------------------------------------------------- #


def test_transport_timeout():
    """When select returns no ready fd, MCPError mentions the timeout."""
    transport = StdioTransport(command=["slow-server"], server_name="slow", timeout_seconds=1)
    fake = _fake_popen(stdout_lines=["{}"])
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    with patch(
        "select.select",
        return_value=([], [], []),  # nothing ready => timeout
    ):
        with pytest.raises(MCPError, match="timed out"):
            transport.send_request({"method": "ping"})
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — empty response
# --------------------------------------------------------------------------- #


def test_transport_empty_response():
    """An empty readline (EOF) is wrapped as MCPError 'closed connection'."""
    transport = StdioTransport(command=["echo"], server_name="s")
    fake = _fake_popen(stdout_lines=[""])  # readline returns ""
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    with patch(
        "select.select",
        return_value=([transport._proc.stdout], [], []),
    ):
        with pytest.raises(MCPError, match="closed connection"):
            transport.send_request({"method": "ping"})
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — malformed JSON response
# --------------------------------------------------------------------------- #


def test_transport_malformed_response():
    """A non-JSON response line raises MCPError mentioning 'invalid JSON'."""
    transport = StdioTransport(command=["echo"], server_name="s")
    fake = _fake_popen(stdout_lines=["this is not json\n"])
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    with patch(
        "select.select",
        return_value=([transport._proc.stdout], [], []),
    ):
        with pytest.raises(MCPError, match="invalid JSON"):
            transport.send_request({"method": "ping"})
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — JSON-RPC error response
# --------------------------------------------------------------------------- #


def test_transport_jsonrpc_error_response():
    """A JSON-RPC error response raises MCPError with the error message."""
    transport = StdioTransport(command=["echo"], server_name="s")
    error_payload = json.dumps({"error": {"code": -32601, "message": "method not found"}}) + "\n"
    fake = _fake_popen(stdout_lines=[error_payload])
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    with patch(
        "select.select",
        return_value=([transport._proc.stdout], [], []),
    ):
        with pytest.raises(MCPError, match="method not found"):
            transport.send_request({"method": "ping"})
    transport.stop()


# --------------------------------------------------------------------------- #
# send_request() — successful init handshake
# --------------------------------------------------------------------------- #


def test_transport_init_handshake():
    """A successful initialize request returns the result dict."""
    transport = StdioTransport(command=["echo"], server_name="s")
    init_result = {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "test-server", "version": "1.0"},
    }
    response_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": init_result}) + "\n"
    fake = _fake_popen(stdout_lines=[response_line])
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
    with patch(
        "select.select",
        return_value=([transport._proc.stdout], [], []),
    ):
        result = transport.send_request(request)
    assert result == init_result
    assert result["protocolVersion"] == "2024-11-05"
    # Verify the request was actually written to stdin.
    transport._proc.stdin.write.assert_called_once()
    written = transport._proc.stdin.write.call_args[0][0]
    assert json.loads(written)["method"] == "initialize"
    transport.stop()


def test_transport_is_connected_after_start():
    """is_connected is True after start() and False after stop()."""
    transport = StdioTransport(command=["echo"], server_name="s")
    fake = _fake_popen(stdout_lines=["{}"])
    with patch("acp.federation.transport.subprocess.Popen", return_value=fake):
        transport.start()
    assert transport.is_connected is True
    transport.stop()
    assert transport.is_connected is False


# --------------------------------------------------------------------------- #
# create_transport factory
# --------------------------------------------------------------------------- #


def test_create_transport_stdio_requires_command():
    """create_transport rejects a stdio config without a command."""
    with pytest.raises(MCPError, match="requires 'command'"):
        create_transport({"transport": "stdio", "name": "s"})


def test_create_transport_unknown_transport():
    """create_transport rejects an unknown transport type."""
    with pytest.raises(MCPError, match="unknown transport"):
        create_transport({"transport": "carrier-pigeon", "name": "s"})


def test_create_transport_returns_stdio():
    """create_transport builds a StdioTransport for a stdio config."""
    t = create_transport({"transport": "stdio", "name": "s", "command": ["echo"]})
    assert isinstance(t, StdioTransport)
    assert t.command == ["echo"]
    assert t.server_name == "s"


# --------------------------------------------------------------------------- #
# SSE transport — malformed/empty SSE response parsing
# --------------------------------------------------------------------------- #


def test_sse_parse_no_json_event():
    """An SSE response with no parseable JSON event raises MCPError."""
    with pytest.raises(MCPError, match="no parseable JSON-RPC event"):
        SSETransport._parse_sse_response(": keepalive\n\n")
