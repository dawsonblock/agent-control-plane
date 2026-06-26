"""MCP client — discover and call tools from federated MCP servers.

This module implements a minimal MCP client that communicates with MCP
servers over stdio (the standard MCP transport for local processes),
HTTP, or SSE. It does NOT require the ``mcp`` PyPI package — it uses
the JSON-RPC protocol directly, keeping ACP's dependency surface minimal.

MCP protocol reference: https://modelcontextprotocol.io/specification

Supported operations:
  - ``initialize``: handshake with the MCP server
  - ``tools/list``: discover available tools
  - ``tools/call``: invoke a tool with arguments

v0.6.9: stdio transport only (subprocess stdin/stdout).
v0.7.0 (Phase 3.1): pluggable transports via :mod:`acp.federation.transport`.
  - stdio: subprocess over stdin/stdout (original)
  - http: JSON-RPC over HTTP POST
  - sse: JSON-RPC over Server-Sent Events
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acp.federation.transport import (
    MCPError,
    Transport,
    create_transport,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


@dataclass
class MCPTool:
    """A tool discovered from an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_prompt_line(self) -> str:
        """One-line description for injection into the agent prompt."""
        desc = self.description or "(no description)"
        return f"  - {self.name}: {desc}"


@dataclass
class MCPToolResult:
    """Result of calling an MCP tool."""

    tool_name: str
    server_name: str
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0


# --------------------------------------------------------------------------- #
# MCP Client — single server connection via pluggable transport
# --------------------------------------------------------------------------- #


class MCPClient:
    """JSON-RPC 2.0 client for a single MCP server.

    v0.6.9: stdio transport only (subprocess stdin/stdout).
    v0.7.0: accepts any :class:`Transport` implementation (stdio, http, sse).

    The client handles the JSON-RPC protocol (request IDs, method names,
    response parsing). The transport handles the wire-level details
    (subprocess pipes, HTTP requests, SSE streams).

    Usage::

        client = MCPClient("my-server", transport)
        client.start()
        tools = client.list_tools()
        result = client.call_tool("search", {"query": "auth"})
        client.stop()
    """

    def __init__(
        self,
        name: str,
        transport: Transport,
    ) -> None:
        self.name = name
        self._transport = transport
        self._request_id = 0
        self._initialized = False

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        cwd: Path | None = None,
    ) -> MCPClient:
        """Create an MCPClient from a server config dict.

        Uses :func:`create_transport` to build the appropriate transport
        based on the ``transport`` field (default: "stdio").
        """
        transport = create_transport(config, cwd=cwd)
        return cls(name=config.get("name", ""), transport=transport)

    def start(self) -> None:
        """Start the transport and perform the MCP initialize handshake."""
        if self._initialized:
            return
        self._transport.start()
        try:
            self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "acp", "version": "0.7.0"},
            })
            self._initialized = True
        except MCPError as exc:
            self.stop()
            raise MCPError(
                f"MCP server '{self.name}' initialization failed: {exc}"
            ) from exc

    def stop(self) -> None:
        """Stop the transport."""
        self._transport.stop()
        self._initialized = False

    @property
    def is_connected(self) -> bool:
        """Whether the client is connected and initialized."""
        return self._initialized and self._transport.is_connected

    def list_tools(self) -> list[MCPTool]:
        """Discover available tools from the server.

        Returns a list of :class:`MCPTool`. If the server is not
        started, starts it first. Returns an empty list on failure
        (graceful degradation).
        """
        if not self._initialized:
            try:
                self.start()
            except MCPError as exc:
                logger.warning("MCP server '%s' failed to start: %s", self.name, exc)
                return []
        try:
            response = self._send_request("tools/list", {})
            tools_data = response.get("tools", [])
            return [
                MCPTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                )
                for t in tools_data
            ]
        except MCPError as exc:
            logger.warning("MCP server '%s' tools/list failed: %s", self.name, exc)
            return []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPToolResult:
        """Call a tool on the MCP server.

        Returns a :class:`MCPToolResult`. Does not raise — errors are
        captured in the result's ``error`` field.
        """
        if not self._initialized:
            self.start()
        start = time.monotonic()
        try:
            response = self._send_request("tools/call", {
                "name": tool_name,
                "arguments": arguments or {},
            })
            duration_ms = int((time.monotonic() - start) * 1000)
            # MCP tool results have a "content" array of content blocks.
            content = response.get("content", [])
            output_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    output_parts.append(block.get("text", ""))
            output = "\n".join(output_parts) if output_parts else json.dumps(response)
            return MCPToolResult(
                tool_name=tool_name,
                server_name=self.name,
                success=True,
                output=output,
                duration_ms=duration_ms,
            )
        except MCPError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return MCPToolResult(
                tool_name=tool_name,
                server_name=self.name,
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
            )

    # ------------------------------------------------------------------ #
    # Internal: JSON-RPC protocol
    # ------------------------------------------------------------------ #

    def _send_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a JSON-RPC request via the transport and return the result."""
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        return self._transport.send_request(request)


# --------------------------------------------------------------------------- #
# Federation manager — orchestrates multiple MCP servers
# --------------------------------------------------------------------------- #


class FederationManager:
    """Manages multiple MCP servers for agent federation.

    Discovers tools from all configured servers, injects them into the
    agent prompt, and proxies tool calls. The agent never touches the
    network — all calls go through this manager.

    Usage::

        fm = FederationManager(servers_config)
        fm.start_all()
        tools = fm.discover_tools()
        prompt_section = fm.build_prompt_section(tools)
        result = fm.call_tool("server1", "search", {"q": "auth"})
        fm.stop_all()
    """

    def __init__(
        self,
        servers: list[dict[str, Any]],
        cwd: Path | None = None,
    ) -> None:
        """Initialize with a list of server config dicts.

        Each dict has:
          - ``name``: server name (for identification in events/prompts)
          - ``transport``: "stdio" (default), "http", or "sse"
          - ``command``: list of strings — the command to spawn the server
            (stdio only)
          - ``url``: server URL (http/sse only)
          - ``headers``: optional HTTP headers (http/sse only)
          - ``env``: optional dict of environment variables (stdio)
          - ``timeout_seconds``: optional per-request timeout (default 30)
        """
        self._clients: dict[str, MCPClient] = {}
        self._transport_types: dict[str, str] = {}
        for s in servers:
            name = s.get("name", "")
            if not name:
                continue
            transport_type = s.get("transport", "stdio")
            # For stdio, command is required. For http/sse, url is required.
            if transport_type == "stdio" and not s.get("command"):
                continue
            if transport_type in ("http", "sse") and not s.get("url"):
                continue
            try:
                self._clients[name] = MCPClient.from_config(s, cwd=cwd)
                self._transport_types[name] = transport_type
            except MCPError as exc:
                logger.warning("Failed to create MCP client '%s': %s", name, exc)

    def start_all(self) -> dict[str, dict[str, Any]]:
        """Start all configured MCP servers.

        Returns a dict mapping server name → connection metadata for
        servers that connected successfully. This metadata is intended
        to be used by the caller to emit ``federation.server_connected``
        events.
        """
        connected: dict[str, dict[str, Any]] = {}
        for client in self._clients.values():
            try:
                client.start()
                transport_type = self._transport_types.get(client.name, "stdio")
                connected[client.name] = {
                    "transport": transport_type,
                    "server_name": client.name,
                }
            except MCPError as exc:
                logger.warning("Failed to start MCP server '%s': %s", client.name, exc)
        return connected

    def stop_all(self) -> None:
        """Stop all MCP servers."""
        for client in self._clients.values():
            client.stop()

    def discover_tools(self) -> dict[str, list[MCPTool]]:
        """Discover tools from all servers.

        Returns a dict mapping server name → list of tools. Servers
        that fail to respond are silently skipped (graceful
        degradation).
        """
        all_tools: dict[str, list[MCPTool]] = {}
        for name, client in self._clients.items():
            try:
                tools = client.list_tools()
                if tools:
                    all_tools[name] = tools
            except MCPError as exc:
                logger.warning("MCP server '%s' discovery failed: %s", name, exc)
        return all_tools

    def build_prompt_section(
        self,
        tools_by_server: dict[str, list[MCPTool]] | None = None,
    ) -> str:
        """Build the prompt section listing federated tools.

        This is injected into the agent prompt so the agent knows what
        federated capabilities it can request. The agent requests a
        tool call by emitting a structured line in its output; ACP
        proxies the actual call.
        """
        if tools_by_server is None:
            tools_by_server = self.discover_tools()
        if not tools_by_server:
            return ""

        lines = ["\n\nFederated capabilities (via MCP):"]
        lines.append(
            "  You can request these tools by printing a line in the format:"
        )
        lines.append("  ACP_FEDERATION_CALL: <server> <tool> {<json_arguments>}")
        lines.append("  The control plane will proxy the call — you do NOT have network access.")
        lines.append("")
        for server, tools in sorted(tools_by_server.items()):
            lines.append(f"  [{server}]")
            for tool in tools:
                lines.append(f"    {tool.to_prompt_line().strip()}")
        return "\n".join(lines) + "\n"

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPToolResult:
        """Proxy a tool call to a specific MCP server."""
        client = self._clients.get(server_name)
        if client is None:
            return MCPToolResult(
                tool_name=tool_name,
                server_name=server_name,
                success=False,
                error=f"Unknown MCP server: {server_name}",
            )
        return client.call_tool(tool_name, arguments)

    @property
    def server_names(self) -> list[str]:
        """Names of all configured servers."""
        return list(self._clients.keys())

    def __enter__(self) -> FederationManager:
        self.start_all()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop_all()
