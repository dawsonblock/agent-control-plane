"""MITM egress proxy daemon (v0.8.0, Phase 1.2).

When ``proxy.enabled`` is True in the repo config, ACP starts a local
HTTP proxy that intercepts all agent network traffic. The proxy:

  1. Logs every request to an :class:`EgressLogger` instance.
  2. Blocks requests to domains not in ``proxy.allowed_domains``.
  3. Forwards allowed requests to the upstream server.

The proxy is a lightweight Python HTTP server (no external dependency
on mitmproxy). It handles CONNECT (HTTPS) and GET/POST/PUT/DELETE (HTTP)
by forwarding to the upstream server and recording the domain, method,
path, and status code.

Usage::

    daemon = EgressProxyDaemon(
        port=8080,
        allowed_domains=["pypi.org", "github.com"],
        egress_logger=EgressLogger(),
    )
    daemon.start()  # starts in a background thread
    # ... agent runs with HTTP_PROXY=http://127.0.0.1:8080 ...
    daemon.stop()

For HTTPS, the proxy uses the CONNECT method to establish a tunnel.
The domain is logged but the encrypted payload is not inspected (true
MITM would require certificate generation, which is out of scope for
the logging-only use case).

Real E2E tests are gated behind ``ACP_RUN_REAL_PROXY=1`` to avoid
network side effects in the default test suite.
"""

from __future__ import annotations

import http.server
import logging
import socket
import socketserver
import threading
import time
import urllib.parse
from typing import Any

from acp.egress import EgressLogger

logger = logging.getLogger(__name__)


class _ProxyRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler that logs egress and enforces the allowlist.

    Handles both plain HTTP (GET/POST/etc.) and HTTPS (CONNECT tunneling).
    For HTTP, the request is forwarded to the upstream server and the
    response is relayed back. For HTTPS, a blind tunnel is established
    (the encrypted stream is relayed without inspection — only the domain
    is logged).
    """

    # Class-level attributes set by EgressProxyDaemon before serving.
    egress_logger: EgressLogger | None = None
    allowed_domains: set[str] = set()
    _lock: threading.Lock = threading.Lock()

    def _check_allowed(self, domain: str) -> bool:
        """Return True if the domain is in the allowlist."""
        domain_clean = domain.lower().strip()
        # Strip port for comparison.
        if ":" in domain_clean:
            domain_clean = domain_clean.split(":")[0]
        return domain_clean in self.allowed_domains

    def _log_request(
        self, domain: str, method: str, path: str, status_code: int, blocked: bool = False
    ) -> None:
        """Log an egress event to the EgressLogger (thread-safe)."""
        if self.egress_logger is None:
            return
        with self._lock:
            self.egress_logger.log_request(
                domain,
                method=method,
                path=path,
                status_code=status_code,
                blocked=blocked,
            )

    def do_CONNECT(self) -> None:  # noqa: N802 — stdlib naming convention
        """Handle HTTPS CONNECT requests — establish a tunnel."""
        # The host:port is in self.path.
        host_port = self.path
        domain = host_port.split(":")[0] if ":" in host_port else host_port

        if not self._check_allowed(domain):
            self._log_request(domain, "CONNECT", "/", 403, blocked=True)
            self.send_error(403, "Blocked by ACP egress policy")
            return

        # Establish the tunnel.
        try:
            host = host_port.split(":")[0]
            port = int(host_port.split(":")[1]) if ":" in host_port else 443
            upstream = socket.create_connection((host, port), timeout=30)
        except OSError as exc:
            self._log_request(domain, "CONNECT", "/", 502, blocked=False)
            self.send_error(502, f"Cannot connect to upstream: {exc}")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()

        # Relay data between client and upstream.
        self._relay(self.connection, upstream)
        self._log_request(domain, "CONNECT", "/", 200, blocked=False)

    def _handle_http_method(self, method: str) -> None:
        """Handle plain HTTP methods (GET, POST, PUT, DELETE, etc.)."""
        # Parse the full URL — proxy requests have absolute URLs.
        parsed = urllib.parse.urlparse(self.path)
        domain = parsed.hostname or ""
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"

        if not domain:
            self.send_error(400, "Bad request — no domain in URL")
            return

        if not self._check_allowed(domain):
            self._log_request(domain, method, path, 403, blocked=True)
            self.send_error(403, "Blocked by ACP egress policy")
            return

        # Read the request body (if any).
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Forward to the upstream server.
        try:
            upstream_sock = socket.create_connection((domain, port), timeout=30)
        except OSError as exc:
            self._log_request(domain, method, path, 502, blocked=False)
            self.send_error(502, f"Cannot connect to upstream: {exc}")
            return

        try:
            # Build the forwarded request.
            req_line = f"{method} {path} HTTP/1.1\r\n"
            headers = f"Host: {domain}\r\n"
            for key, val in self.headers.items():
                if key.lower() in ("host", "proxy-connection", "connection"):
                    continue
                headers += f"{key}: {val}\r\n"
            if body:
                headers += f"Content-Length: {len(body)}\r\n"
            headers += "Connection: close\r\n\r\n"

            upstream_sock.sendall(req_line.encode() + headers.encode() + (body or b""))

            # Read the response and relay it back.
            response_data = b""
            while True:
                chunk = upstream_sock.recv(65536)
                if not chunk:
                    break
                response_data += chunk
                self.connection.sendall(chunk)

            # Extract status code from the response.
            status_code = 200
            if response_data:
                status_line = response_data.split(b"\r\n", 1)[0]
                parts = status_line.split(b" ", 2)
                if len(parts) >= 2:
                    try:
                        status_code = int(parts[1])
                    except ValueError:
                        pass

            self._log_request(domain, method, path, status_code, blocked=False)
        except OSError as exc:
            self._log_request(domain, method, path, 502, blocked=False)
            logger.warning("egress proxy: upstream error for %s: %s", domain, exc)
        finally:
            upstream_sock.close()

    def do_GET(self) -> None:  # noqa: N802
        self._handle_http_method("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle_http_method("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_http_method("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_http_method("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_http_method("PATCH")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_http_method("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle_http_method("OPTIONS")

    def _relay(self, sock_a: socket.socket, sock_b: socket.socket) -> None:
        """Relay data between two sockets until one closes."""
        sockets = [sock_a, sock_b]
        while True:
            try:
                # Use select for simplicity.
                import select

                readable, _, _ = select.select(sockets, [], [], 30)
                if not readable:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    other = sock_b if s is sock_a else sock_a
                    other.sendall(data)
            except OSError:
                return

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging — we log via EgressLogger instead."""
        pass


class _ThreadingHTTPProxyServer(socketserver.ThreadingTCPServer):
    """Threaded TCP server for the proxy."""

    allow_reuse_address = True
    daemon_threads = True


class EgressProxyDaemon:
    """Manages the lifecycle of the local MITM egress proxy.

    Starts the proxy in a background thread, provides the proxy URL
    for environment variable injection, and stops it cleanly.

    Attributes:
        port: The port the proxy listens on.
        allowed_domains: Set of domains the agent may access.
        egress_logger: The EgressLogger that records all egress events.
    """

    def __init__(
        self,
        *,
        port: int = 8080,
        allowed_domains: list[str] | None = None,
        egress_logger: EgressLogger | None = None,
    ) -> None:
        self.port = port
        self.allowed_domains = {d.lower().strip() for d in (allowed_domains or [])}
        self.egress_logger = egress_logger or EgressLogger()
        self._server: _ThreadingHTTPProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def proxy_url(self) -> str:
        """The proxy URL for HTTP_PROXY/HTTPS_PROXY env vars."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        """True if the proxy server is currently running."""
        return self._started and self._server is not None

    def start(self) -> None:
        """Start the proxy server in a background thread."""
        if self._started:
            return

        # Configure the request handler class with our logger + allowlist.
        _ProxyRequestHandler.egress_logger = self.egress_logger
        _ProxyRequestHandler.allowed_domains = self.allowed_domains

        self._server = _ThreadingHTTPProxyServer(
            ("127.0.0.1", self.port),
            _ProxyRequestHandler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="acp-egress-proxy",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        # Give the server a moment to bind.
        time.sleep(0.1)
        logger.info(
            "egress proxy started on port %d (allowed: %s)",
            self.port,
            sorted(self.allowed_domains) if self.allowed_domains else "(none)",
        )

    def stop(self) -> None:
        """Stop the proxy server and clean up."""
        if not self._started or self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self._started = False
        logger.info("egress proxy stopped")

    def get_proxy_env_vars(self) -> dict[str, str]:
        """Return the env vars to inject into the agent's environment."""
        if not self.is_running:
            return {}
        return {
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
            "http_proxy": self.proxy_url,
            "https_proxy": self.proxy_url,
        }

    def __enter__(self) -> EgressProxyDaemon:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
