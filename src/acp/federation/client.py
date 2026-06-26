"""MCP client — discover and call tools from federated MCP servers.

This module implements a minimal MCP client that communicates with MCP
servers over stdio (the standard MCP transport for local processes).
It does NOT require the ``mcp`` PyPI package — it uses the JSON-RPC
protocol directly over subprocess stdin/stdout, keeping ACP's
dependency surface minimal.

MCP protocol reference: https://modelcontextprotocol.io/specification

Supported operations:
  - ``initialize``: handshake with the MCP server
  - ``tools/list``: discover available tools
  - ``tools/call``: invoke a tool with arguments

Each MCP server is a subprocess that speaks JSON-RPC 2.0 over
stdin/stdout. The server is spawned on demand and kept alive for the
duration of the ACP run.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


class MCPError(Exception):
    """Raised when an MCP server operation fails."""


# --------------------------------------------------------------------------- #
# MCP Client — single server connection over stdio
# --------------------------------------------------------------------------- #


class MCPClient:
    """JSON-RPC 2.0 client for a single MCP server over stdio.

    The server is a subprocess spawned from a command (e.g.
    ``["python", "-m", "my_mcp_server"]``). Communication is via
    newline-delimited JSON-RPC messages on stdin/stdout.

    Usage::

        client = MCPClient("my-server", ["python", "-m", "my_mcp_server"])
        client.start()
        tools = client.list_tools()
        result = client.call_tool("search", {"query": "auth"})
        client.stop()
    """

    def __init__(
        self,
        name: str,
        command: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.timeout_seconds = timeout_seconds
        self._proc: subprocess.Popen | None = None
        self._request_id = 0
        self._initialized = False

    def start(self) -> None:
        """Spawn the MCP server subprocess and perform the initialize handshake."""
        if self._proc is not None:
            return  # already started
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
                f"MCP server '{self.name}': command not found: {self.command[0]}"
            ) from exc

        # Perform the MCP initialize handshake.
        try:
            self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "acp", "version": "0.6.9"},
            })
            self._initialized = True
        except MCPError as exc:
            self.stop()
            raise MCPError(
                f"MCP server '{self.name}' initialization failed: {exc}"
            ) from exc

    def stop(self) -> None:
        """Terminate the MCP server subprocess."""
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
            self._initialized = False

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
    # Internal: JSON-RPC transport
    # ------------------------------------------------------------------ #

    def _send_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and read the response."""
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError(f"MCP server '{self.name}' is not running")
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(request) + "\n"
        assert self._proc.stdin is not None
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

        # Read the response line with a timeout.
        assert self._proc.stdout is not None
        # Use a simple readline with poll-based timeout.
        import select
        ready, _, _ = select.select(
            [self._proc.stdout], [], [], self.timeout_seconds,
        )
        if not ready:
            raise MCPError(
                f"MCP server '{self.name}' timed out after {self.timeout_seconds}s "
                f"waiting for response to '{method}'"
            )
        response_line = self._proc.stdout.readline()
        if not response_line:
            raise MCPError(
                f"MCP server '{self.name}' closed connection during '{method}'"
            )
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as exc:
            raise MCPError(
                f"MCP server '{self.name}' returned invalid JSON: {exc}"
            ) from exc
        if "error" in response:
            error = response["error"]
            raise MCPError(
                f"MCP server '{self.name}' error on '{method}': "
                f"{error.get('message', error)}"
            )
        return response.get("result", {})


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
          - ``command``: list of strings — the command to spawn the server
          - ``env``: optional dict of environment variables
          - ``timeout_seconds``: optional per-request timeout (default 30)
        """
        self._clients: dict[str, MCPClient] = {}
        for s in servers:
            name = s.get("name", "")
            command = s.get("command", [])
            if not name or not command:
                continue
            self._clients[name] = MCPClient(
                name=name,
                command=list(command),
                cwd=cwd,
                env=s.get("env"),
                timeout_seconds=s.get("timeout_seconds", 30),
            )

    def start_all(self) -> None:
        """Start all configured MCP servers."""
        for client in self._clients.values():
            try:
                client.start()
            except MCPError as exc:
                logger.warning("Failed to start MCP server '%s': %s", client.name, exc)

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
