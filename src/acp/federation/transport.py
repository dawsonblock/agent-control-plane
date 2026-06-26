"""MCP transports — pluggable transport layer for MCP client communication.

v0.7.0 (Phase 3.1): Extracts the transport layer from :class:`MCPClient`
so that MCP servers can be reached over stdio (local subprocess), HTTP
(request-response), or SSE (streaming). The transport handles the
wire-level details; the client handles the JSON-RPC protocol.

Transport interface:

    class Transport(Protocol):
        def send_request(self, request: dict) -> dict: ...
        def start(self) -> None: ...
        def stop(self) -> None: ...
        @property
        def is_connected(self) -> bool: ...

The stdio transport (the original, used by v0.6.9) spawns a subprocess
and communicates over stdin/stdout. The HTTP transport sends JSON-RPC
requests as HTTP POST to a server URL. The SSE transport uses Server-Sent
Events for streaming responses.

All transports return the JSON-RPC response dict (the ``result`` field)
or raise :class:`MCPError` on failure.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class MCPError(Exception):
    """Raised when an MCP transport operation fails."""


# --------------------------------------------------------------------------- #
# Transport protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class Transport(Protocol):
    """Abstract transport interface for MCP communication.

    A transport handles the wire-level details of sending and receiving
    JSON-RPC 2.0 messages. The MCP client uses the transport to send
    requests and receive responses.

    All transports must implement:
      - ``start()``: establish the connection (spawn process, open socket, etc.)
      - ``stop()``: close the connection cleanly
      - ``send_request(request: dict) -> dict``: send a JSON-RPC request
        and return the ``result`` field from the response
      - ``is_connected``: whether the transport is currently connected
    """

    def start(self) -> None:
        """Establish the connection."""
        ...

    def stop(self) -> None:
        """Close the connection cleanly."""
        ...

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result dict.

        Raises :class:`MCPError` on any failure (timeout, connection
        error, JSON-RPC error response).
        """
        ...

    @property
    def is_connected(self) -> bool:
        """Whether the transport is currently connected."""
        ...


# --------------------------------------------------------------------------- #
# Stdio transport — subprocess over stdin/stdout (the original v0.6.9 transport)
# --------------------------------------------------------------------------- #


class StdioTransport:
    """JSON-RPC 2.0 over subprocess stdin/stdout.

    This is the standard MCP transport for local processes. The server
    is spawned as a subprocess, and communication is via newline-delimited
    JSON-RPC messages on stdin/stdout.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        server_name: str = "",
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.timeout_seconds = timeout_seconds
        self.server_name = server_name
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
                env=self.env,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MCPError(
                f"MCP server '{self.server_name}': command not found: {self.command[0]}"
            ) from exc

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._proc = None

    @property
    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError(f"MCP server '{self.server_name}' is not running")
        line = json.dumps(request) + "\n"
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(
                f"MCP server '{self.server_name}' closed connection during write: {exc}"
            ) from exc

        import select
        assert self._proc.stdout is not None
        ready, _, _ = select.select(
            [self._proc.stdout], [], [], self.timeout_seconds,
        )
        if not ready:
            raise MCPError(
                f"MCP server '{self.server_name}' timed out after "
                f"{self.timeout_seconds}s"
            )
        response_line = self._proc.stdout.readline()
        if not response_line:
            raise MCPError(
                f"MCP server '{self.server_name}' closed connection"
            )
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as exc:
            raise MCPError(
                f"MCP server '{self.server_name}' returned invalid JSON: {exc}"
            ) from exc
        if "error" in response:
            error = response["error"]
            raise MCPError(
                f"MCP server '{self.server_name}' error: "
                f"{error.get('message', error)}"
            )
        return response.get("result", {})


# --------------------------------------------------------------------------- #
# HTTP transport — JSON-RPC 2.0 over HTTP POST
# --------------------------------------------------------------------------- #


class HTTPTransport:
    """JSON-RPC 2.0 over HTTP POST.

    Used for MCP servers that expose an HTTP endpoint (e.g., a remote
    MCP server behind a reverse proxy). Each JSON-RPC request is sent
    as an HTTP POST to the server URL, and the response body is the
    JSON-RPC response.

    This transport uses :mod:`urllib` from the standard library — no
    external HTTP client dependency is required.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        server_name: str = "",
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds
        self.server_name = server_name
        self._connected = False

    def start(self) -> None:
        # HTTP is stateless — "starting" just means we verify the URL
        # is reachable. We try a HEAD request first, but some MCP servers
        # don't support HEAD (return 405). In that case, we fall back to
        # a GET request. If both fail, the server is unreachable.
        if self._connected:
            return
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                self.url, method="HEAD",
                headers=self.headers,
            )
            urllib.request.urlopen(req, timeout=self.timeout_seconds)
            self._connected = True
        except urllib.error.HTTPError as exc:
            # 405 Method Not Allowed — server doesn't support HEAD.
            # Fall back to a GET request to verify connectivity.
            if exc.code == 405:
                try:
                    req = urllib.request.Request(
                        self.url, method="GET",
                        headers=self.headers,
                    )
                    urllib.request.urlopen(req, timeout=self.timeout_seconds)
                    self._connected = True
                    return
                except Exception as exc2:  # noqa: BLE001
                    raise MCPError(
                        f"MCP HTTP server '{self.server_name}' unreachable at "
                        f"{self.url}: {exc2}"
                    ) from exc2
            # Other HTTP errors (4xx, 5xx) — server is reachable but unhealthy.
            raise MCPError(
                f"MCP HTTP server '{self.server_name}' unreachable at "
                f"{self.url}: HTTP {exc.code}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise MCPError(
                f"MCP HTTP server '{self.server_name}' unreachable at "
                f"{self.url}: {exc}"
            ) from exc

    def stop(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._connected:
            self.start()
        import urllib.request
        body = json.dumps(request).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.headers}
        try:
            req = urllib.request.Request(
                self.url, data=body, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                response = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise MCPError(
                f"MCP HTTP server '{self.server_name}' request failed: {exc}"
            ) from exc
        if "error" in response:
            error = response["error"]
            raise MCPError(
                f"MCP HTTP server '{self.server_name}' error: "
                f"{error.get('message', error)}"
            )
        return response.get("result", {})


# --------------------------------------------------------------------------- #
# SSE transport — JSON-RPC 2.0 over Server-Sent Events
# --------------------------------------------------------------------------- #


class SSETransport:
    """JSON-RPC 2.0 over Server-Sent Events (SSE).

    Used for MCP servers that stream responses via SSE. The client opens
    a long-lived SSE connection to the server's events endpoint and sends
    requests via HTTP POST to the server's request endpoint. Responses
    arrive as SSE events with ``data`` fields containing the JSON-RPC
    response.

    This transport uses :mod:`urllib` for POST requests and a simple
    line-based SSE parser for streaming responses. It does not require
    any external SSE client library.
    """

    def __init__(
        self,
        url: str,
        *,
        events_path: str = "/events",
        request_path: str = "/request",
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        server_name: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self.events_url = self.url + events_path
        self.request_url = self.url + request_path
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds
        self.server_name = server_name
        self._connected = False

    def start(self) -> None:
        if self._connected:
            return
        # For SSE, "starting" means verifying the events endpoint exists.
        # We don't open the SSE stream yet — that happens per-request
        # to avoid holding a connection open indefinitely.
        import urllib.request
        try:
            req = urllib.request.Request(
                self.events_url, method="HEAD",
                headers={"Accept": "text/event-stream", **self.headers},
            )
            urllib.request.urlopen(req, timeout=self.timeout_seconds)
            self._connected = True
        except Exception as exc:  # noqa: BLE001
            raise MCPError(
                f"MCP SSE server '{self.server_name}' unreachable at "
                f"{self.events_url}: {exc}"
            ) from exc

    def stop(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._connected:
            self.start()
        # For SSE, we send the request via POST and read the response
        # from the SSE stream. In practice, many SSE MCP servers return
        # the response directly in the POST response body (simpler than
        # full SSE streaming for request-response patterns).
        import urllib.request
        body = json.dumps(request).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self.headers,
        }
        try:
            req = urllib.request.Request(
                self.request_url, data=body, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8")
                if "text/event-stream" in content_type:
                    response = self._parse_sse_response(raw)
                else:
                    response = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise MCPError(
                f"MCP SSE server '{self.server_name}' request failed: {exc}"
            ) from exc
        if "error" in response:
            error = response["error"]
            raise MCPError(
                f"MCP SSE server '{self.server_name}' error: "
                f"{error.get('message', error)}"
            )
        return response.get("result", {})

    @staticmethod
    def _parse_sse_response(raw: str) -> dict[str, Any]:
        """Parse an SSE response stream and extract the first JSON-RPC result.

        SSE events are delimited by blank lines. Each event has ``data:``
        lines whose concatenation forms the event payload. Comment lines
        (starting with ``:``) and other field lines (``event:``, ``id:``,
        ``retry:``) are ignored per the SSE specification. We parse the
        first event with a JSON payload and return it.
        """
        import re
        # Split on blank lines — handle \n\n, \r\n\r\n, and \r\r per SSE spec.
        for block in re.split(r"\r\n\r\n|\r\r|\n\n", raw):
            data_lines = []
            for line in block.splitlines():
                if line.startswith(":"):
                    # SSE comment line — ignore per spec.
                    continue
                if line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line.startswith("data:"):
                    # Handle "data:" with no content after it (empty data line).
                    data_lines.append(line[5:] if len(line) > 5 else "")
                # Other SSE fields (event:, id:, retry:) are ignored —
                # we only care about the data payload.
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue
        raise MCPError("SSE response contained no parseable JSON-RPC event")


# --------------------------------------------------------------------------- #
# Transport factory
# --------------------------------------------------------------------------- #


def create_transport(
    config: dict[str, Any],
    *,
    cwd: Path | None = None,
) -> Transport:
    """Create a transport from a server config dict.

    The config dict must have:
      - ``transport``: "stdio" (default), "http", or "sse"
      - For stdio: ``command`` (list of strings)
      - For http/sse: ``url`` (string)
      - Optional: ``headers``, ``timeout_seconds``, ``env``, ``name``
    """
    transport_type = config.get("transport", "stdio")
    name = config.get("name", "")
    timeout = config.get("timeout_seconds", 30)

    if transport_type == "stdio":
        command = config.get("command", [])
        if not command:
            raise MCPError(f"MCP server '{name}': stdio transport requires 'command'")
        return StdioTransport(
            command=list(command),
            cwd=cwd,
            env=config.get("env"),
            timeout_seconds=timeout,
            server_name=name,
        )
    elif transport_type == "http":
        url = config.get("url", "")
        if not url:
            raise MCPError(f"MCP server '{name}': http transport requires 'url'")
        return HTTPTransport(
            url=url,
            headers=config.get("headers"),
            timeout_seconds=timeout,
            server_name=name,
        )
    elif transport_type == "sse":
        url = config.get("url", "")
        if not url:
            raise MCPError(f"MCP server '{name}': sse transport requires 'url'")
        return SSETransport(
            url=url,
            events_path=config.get("events_path", "/events"),
            request_path=config.get("request_path", "/request"),
            headers=config.get("headers"),
            timeout_seconds=timeout,
            server_name=name,
        )
    else:
        raise MCPError(
            f"MCP server '{name}': unknown transport '{transport_type}'. "
            f"Must be 'stdio', 'http', or 'sse'."
        )
