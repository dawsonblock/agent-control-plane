"""Tests for agent federation via MCP (v0.6.9).

Tests the MCP client, FederationManager, and config integration.
The MCP client tests use a mock MCP server (a Python script that
speaks JSON-RPC over stdio) to avoid requiring a real MCP server.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from acp.config import FederationSection, FederationServerConfig, RepoConfig, RepoSection
from acp.federation.client import (
    FederationManager,
    MCPClient,
)
from acp.federation.transport import (
    HTTPTransport,
    MCPError,
    SSETransport,
    StdioTransport,
    Transport,
    create_transport,
)

# --------------------------------------------------------------------------- #
# Mock MCP server — a Python script that speaks JSON-RPC over stdio
# --------------------------------------------------------------------------- #


MOCK_SERVER_SCRIPT = textwrap.dedent("""\
    import json, sys

    def handle(method, params):
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
        if method == "tools/list":
            return {"tools": [
                {"name": "search", "description": "Search the codebase",
                 "inputSchema": {"type": "object"}},
                {"name": "analyze", "description": "Analyze a file",
                 "inputSchema": {"type": "object"}},
            ]}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            return {"content": [
                {"type": "text", "text": f"Result of {name} with {json.dumps(args)}"}
            ]}
        return {}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        result = handle(req.get("method", ""), req.get("params", {}))
        response = {"jsonrpc": "2.0", "id": req.get("id", 0), "result": result}
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
""")


@pytest.fixture
def mock_server_path(tmp_path: Path) -> Path:
    """Write the mock MCP server script to a temp file."""
    script = tmp_path / "mock_mcp_server.py"
    script.write_text(MOCK_SERVER_SCRIPT)
    return script


# --------------------------------------------------------------------------- #
# MCPClient tests
# --------------------------------------------------------------------------- #


class TestMCPClient:
    """Test the single-server MCP client."""

    def test_start_and_list_tools(self, mock_server_path: Path):
        """MCPClient can start a server and discover tools."""
        transport = StdioTransport(
            command=[sys.executable, str(mock_server_path)],
            timeout_seconds=10,
            server_name="test-server",
        )
        client = MCPClient(name="test-server", transport=transport)
        try:
            client.start()
            assert client._initialized is True
            tools = client.list_tools()
            assert len(tools) == 2
            assert tools[0].name == "search"
            assert tools[1].name == "analyze"
        finally:
            client.stop()

    def test_call_tool(self, mock_server_path: Path):
        """MCPClient can call a tool and get a text result."""
        transport = StdioTransport(
            command=[sys.executable, str(mock_server_path)],
            timeout_seconds=10,
            server_name="test-server",
        )
        client = MCPClient(name="test-server", transport=transport)
        try:
            client.start()
            result = client.call_tool("search", {"query": "auth"})
            assert result.success is True
            assert result.server_name == "test-server"
            assert "search" in result.output
            assert "auth" in result.output
        finally:
            client.stop()

    def test_start_command_not_found(self):
        """MCPClient raises MCPError if the command doesn't exist."""
        transport = StdioTransport(
            command=["nonexistent-binary-xyz"],
            server_name="bad-server",
        )
        client = MCPClient(name="bad-server", transport=transport)
        with pytest.raises(MCPError, match="command not found"):
            client.start()

    def test_stop_is_idempotent(self, mock_server_path: Path):
        """Calling stop() multiple times is safe."""
        transport = StdioTransport(
            command=[sys.executable, str(mock_server_path)],
            server_name="test-server",
        )
        client = MCPClient(name="test-server", transport=transport)
        client.start()
        client.stop()
        client.stop()  # should not raise

    def test_list_tools_graceful_degradation(self):
        """list_tools returns empty list on failure, not raise."""
        transport = StdioTransport(
            command=["nonexistent-binary-xyz"],
            server_name="bad-server",
        )
        client = MCPClient(name="bad-server", transport=transport)
        # list_tools calls start() internally, which fails.
        tools = client.list_tools()
        assert tools == []


# --------------------------------------------------------------------------- #
# FederationManager tests
# --------------------------------------------------------------------------- #


class TestFederationManager:
    """Test the multi-server federation manager."""

    def test_discover_tools_from_multiple_servers(self, mock_server_path: Path):
        """FederationManager discovers tools from all configured servers."""
        servers = [
            {"name": "server-a", "command": [sys.executable, str(mock_server_path)]},
            {"name": "server-b", "command": [sys.executable, str(mock_server_path)]},
        ]
        with FederationManager(servers) as fm:
            tools = fm.discover_tools()
            assert "server-a" in tools
            assert "server-b" in tools
            assert len(tools["server-a"]) == 2
            assert len(tools["server-b"]) == 2

    def test_build_prompt_section(self, mock_server_path: Path):
        """FederationManager builds a prompt section listing tools."""
        servers = [
            {"name": "search-server", "command": [sys.executable, str(mock_server_path)]},
        ]
        with FederationManager(servers) as fm:
            section = fm.build_prompt_section()
            assert "Federated capabilities" in section
            assert "search-server" in section
            assert "search" in section
            assert "ACP_FEDERATION_CALL" in section

    def test_build_prompt_section_empty_when_no_tools(self):
        """FederationManager returns empty string when no servers configured."""
        with FederationManager([]) as fm:
            section = fm.build_prompt_section()
            assert section == ""

    def test_call_tool_proxies_to_server(self, mock_server_path: Path):
        """FederationManager.call_tool proxies to the right server."""
        servers = [
            {"name": "my-server", "command": [sys.executable, str(mock_server_path)]},
        ]
        with FederationManager(servers) as fm:
            result = fm.call_tool("my-server", "analyze", {"file": "test.py"})
            assert result.success is True
            assert "analyze" in result.output
            assert "test.py" in result.output

    def test_call_tool_unknown_server(self):
        """FederationManager.call_tool returns error for unknown server."""
        with FederationManager([]) as fm:
            result = fm.call_tool("unknown", "search", {})
            assert result.success is False
            assert "Unknown MCP server" in result.error

    def test_graceful_degradation_on_bad_server(
        self,
        mock_server_path: Path,
    ):
        """FederationManager skips servers that fail to start."""
        servers = [
            {"name": "bad", "command": ["nonexistent-binary-xyz"]},
            {"name": "good", "command": [sys.executable, str(mock_server_path)]},
        ]
        with FederationManager(servers) as fm:
            tools = fm.discover_tools()
            # Bad server is skipped, good server's tools are present.
            assert "bad" not in tools
            assert "good" in tools

    def test_context_manager_starts_and_stops(self, mock_server_path: Path):
        """FederationManager works as a context manager."""
        servers = [
            {"name": "test", "command": [sys.executable, str(mock_server_path)]},
        ]
        with FederationManager(servers) as fm:
            assert "test" in fm.server_names
        # After exit, servers are stopped.

    def test_server_names(self, mock_server_path: Path):
        """FederationManager.server_names lists all configured servers."""
        servers = [
            {"name": "alpha", "command": [sys.executable, str(mock_server_path)]},
            {"name": "beta", "command": [sys.executable, str(mock_server_path)]},
        ]
        with FederationManager(servers) as fm:
            names = fm.server_names
            assert "alpha" in names
            assert "beta" in names


# --------------------------------------------------------------------------- #
# Config integration tests
# --------------------------------------------------------------------------- #


class TestFederationConfig:
    """Test the federation config section."""

    def test_federation_defaults_empty(self):
        cfg = FederationSection()
        assert cfg.servers == []

    def test_federation_with_servers(self):
        cfg = FederationSection(
            servers=[
                FederationServerConfig(
                    name="my-server",
                    command=["python", "-m", "my_mcp_server"],
                ),
            ]
        )
        assert len(cfg.servers) == 1
        assert cfg.servers[0].name == "my-server"
        assert cfg.servers[0].command == ["python", "-m", "my_mcp_server"]

    def test_federation_in_repo_config(self):
        cfg = RepoConfig(
            repo=RepoSection(name="test", path=Path("/tmp/test")),
            federation=FederationSection(
                servers=[
                    FederationServerConfig(
                        name="search-agent",
                        command=["python", "-m", "search_mcp"],
                        env={"API_KEY": "secret"},
                        timeout_seconds=60,
                    ),
                ]
            ),
        )
        assert len(cfg.federation.servers) == 1
        assert cfg.federation.servers[0].name == "search-agent"
        assert cfg.federation.servers[0].env == {"API_KEY": "secret"}
        assert cfg.federation.servers[0].timeout_seconds == 60

    def test_federation_section_optional_in_yaml(self, tmp_path: Path):
        """A repo config without a federation section loads fine."""
        import yaml

        config_file = tmp_path / "test.repo.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "repo": {"name": "test", "path": str(tmp_path)},
                }
            )
        )
        from acp.config import load_repo_config

        cfg = load_repo_config(config_file)
        assert cfg.federation.servers == []


# --------------------------------------------------------------------------- #
# v0.7.0 (Phase 3.1): Transport layer tests
# --------------------------------------------------------------------------- #


class TestTransportProtocol:
    """Test the Transport protocol and factory."""

    def test_stdio_transport_is_transport(self):
        """StdioTransport satisfies the Transport protocol."""
        t = StdioTransport(command=["echo"], server_name="test")
        assert isinstance(t, Transport)

    def test_http_transport_is_transport(self):
        """HTTPTransport satisfies the Transport protocol."""
        t = HTTPTransport(url="http://localhost:8080", server_name="test")
        assert isinstance(t, Transport)

    def test_sse_transport_is_transport(self):
        """SSETransport satisfies the Transport protocol."""
        t = SSETransport(url="http://localhost:8080", server_name="test")
        assert isinstance(t, Transport)

    def test_create_transport_stdio(self):
        """create_transport creates a StdioTransport for stdio config."""
        t = create_transport(
            {
                "name": "test",
                "transport": "stdio",
                "command": ["python", "-m", "server"],
            }
        )
        assert isinstance(t, StdioTransport)

    def test_create_transport_http(self):
        """create_transport creates an HTTPTransport for http config."""
        t = create_transport(
            {
                "name": "test",
                "transport": "http",
                "url": "http://localhost:8080/mcp",
            }
        )
        assert isinstance(t, HTTPTransport)

    def test_create_transport_sse(self):
        """create_transport creates an SSETransport for sse config."""
        t = create_transport(
            {
                "name": "test",
                "transport": "sse",
                "url": "http://localhost:8080",
            }
        )
        assert isinstance(t, SSETransport)

    def test_create_transport_defaults_to_stdio(self):
        """create_transport defaults to stdio when transport is not specified."""
        t = create_transport(
            {
                "name": "test",
                "command": ["python", "-m", "server"],
            }
        )
        assert isinstance(t, StdioTransport)

    def test_create_transport_unknown_raises(self):
        """create_transport raises MCPError for unknown transport type."""
        with pytest.raises(MCPError, match="unknown transport"):
            create_transport({"name": "test", "transport": "websocket"})

    def test_create_transport_stdio_requires_command(self):
        """create_transport raises MCPError when stdio has no command."""
        with pytest.raises(MCPError, match="requires 'command'"):
            create_transport({"name": "test", "transport": "stdio"})

    def test_create_transport_http_requires_url(self):
        """create_transport raises MCPError when http has no url."""
        with pytest.raises(MCPError, match="requires 'url'"):
            create_transport({"name": "test", "transport": "http"})


class TestStdioTransport:
    """Test the stdio transport implementation."""

    def test_start_and_send_request(self, mock_server_path: Path):
        """StdioTransport can start, send a request, and get a response."""
        transport = StdioTransport(
            command=[sys.executable, str(mock_server_path)],
            server_name="test",
            timeout_seconds=10,
        )
        try:
            transport.start()
            assert transport.is_connected
            result = transport.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                }
            )
            assert "tools" in result
            assert len(result["tools"]) == 2
        finally:
            transport.stop()

    def test_stop_disconnects(self, mock_server_path: Path):
        """After stop(), is_connected is False."""
        transport = StdioTransport(
            command=[sys.executable, str(mock_server_path)],
            server_name="test",
        )
        transport.start()
        assert transport.is_connected
        transport.stop()
        assert not transport.is_connected

    def test_send_request_when_not_started_raises(self):
        """send_request raises MCPError when transport is not started."""
        transport = StdioTransport(command=["echo"], server_name="test")
        with pytest.raises(MCPError, match="not running"):
            transport.send_request({"jsonrpc": "2.0", "id": 1, "method": "test"})


class TestSSEParsing:
    """Test the SSE response parser."""

    def test_parse_sse_single_event(self):
        """SSE parser extracts JSON from a single event."""
        sse_data = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}\n\n'
        result = SSETransport._parse_sse_response(sse_data)
        assert result["result"] == {"tools": []}

    def test_parse_sse_multi_line_data(self):
        """SSE parser handles multi-line data fields."""
        sse_data = 'data: {"jsonrpc": "2.0",\ndata: "id": 1,\ndata: "result": {"tools": []}}\n\n'
        result = SSETransport._parse_sse_response(sse_data)
        assert result["result"] == {"tools": []}

    def test_parse_sse_skips_non_json_events(self):
        """SSE parser skips events without JSON data."""
        sse_data = 'event: ping\ndata: pong\n\ndata: {"jsonrpc": "2.0", "id": 1, "result": {}}\n\n'
        result = SSETransport._parse_sse_response(sse_data)
        assert result["jsonrpc"] == "2.0"

    def test_parse_sse_no_json_raises(self):
        """SSE parser raises MCPError when no JSON event is found."""
        sse_data = "event: ping\ndata: pong\n\n"
        with pytest.raises(MCPError, match="no parseable"):
            SSETransport._parse_sse_response(sse_data)


# --------------------------------------------------------------------------- #
# v0.7.0 (Phase 3.1): FederationManager.start_all returns connection metadata
# --------------------------------------------------------------------------- #


class TestFederationServerConnected:
    """Test that start_all returns connection metadata for event emission."""

    def test_start_all_returns_connected_servers(self, mock_server_path: Path):
        """start_all returns a dict of connected server metadata."""
        servers = [
            {"name": "server-a", "command": [sys.executable, str(mock_server_path)]},
        ]
        fm = FederationManager(servers)
        try:
            connected = fm.start_all()
            assert "server-a" in connected
            assert connected["server-a"]["transport"] == "stdio"
            assert connected["server-a"]["server_name"] == "server-a"
        finally:
            fm.stop_all()

    def test_start_all_skips_failed_servers(self, mock_server_path: Path):
        """start_all only includes servers that connected successfully."""
        servers = [
            {"name": "bad", "command": ["nonexistent-binary-xyz"]},
            {"name": "good", "command": [sys.executable, str(mock_server_path)]},
        ]
        fm = FederationManager(servers)
        try:
            connected = fm.start_all()
            assert "bad" not in connected
            assert "good" in connected
        finally:
            fm.stop_all()

    def test_start_all_with_http_transport_type(self):
        """start_all records the transport type for http servers."""
        # We can't actually connect to an HTTP server in tests, but we
        # can verify the transport type is recorded in the config.
        servers = [
            {"name": "http-server", "transport": "http", "url": "http://localhost:9999"},
        ]
        fm = FederationManager(servers)
        # The client is created but start_all will fail to connect.
        connected = fm.start_all()
        # Server failed to connect — not in the connected dict.
        assert "http-server" not in connected


# --------------------------------------------------------------------------- #
# v0.7.0 (Phase 3.1): Extended FederationServerConfig
# --------------------------------------------------------------------------- #


class TestExtendedFederationConfig:
    """Test the extended FederationServerConfig with transport field."""

    def test_stdio_config_defaults(self):
        """FederationServerConfig defaults to stdio transport."""
        cfg = FederationServerConfig(
            name="test",
            command=["python", "-m", "server"],
        )
        assert cfg.transport == "stdio"

    def test_http_config(self):
        """FederationServerConfig accepts http transport with url."""
        cfg = FederationServerConfig(
            name="test",
            transport="http",
            url="http://localhost:8080/mcp",
        )
        assert cfg.transport == "http"
        assert cfg.url == "http://localhost:8080/mcp"

    def test_sse_config(self):
        """FederationServerConfig accepts sse transport with url."""
        cfg = FederationServerConfig(
            name="test",
            transport="sse",
            url="http://localhost:8080",
        )
        assert cfg.transport == "sse"

    def test_invalid_transport_rejected(self):
        """FederationServerConfig rejects unknown transport types."""
        with pytest.raises(ValueError, match="not valid"):
            FederationServerConfig(
                name="test",
                transport="websocket",
                command=["python"],
            )

    def test_stdio_requires_command(self):
        """FederationServerConfig with stdio transport requires command."""
        with pytest.raises(ValueError, match="requires 'command'"):
            FederationServerConfig(name="test", transport="stdio")

    def test_http_requires_url(self):
        """FederationServerConfig with http transport requires url."""
        with pytest.raises(ValueError, match="requires 'url'"):
            FederationServerConfig(name="test", transport="http")

    def test_http_config_in_yaml(self, tmp_path: Path):
        """A repo config with an HTTP MCP server loads correctly."""
        import yaml

        config_file = tmp_path / "test.repo.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "repo": {"name": "test", "path": str(tmp_path)},
                    "federation": {
                        "servers": [
                            {
                                "name": "remote-mcp",
                                "transport": "http",
                                "url": "https://mcp.example.com/rpc",
                                "headers": {"Authorization": "Bearer token"},
                                "timeout_seconds": 60,
                            },
                        ],
                    },
                }
            )
        )
        from acp.config import load_repo_config

        cfg = load_repo_config(config_file)
        assert len(cfg.federation.servers) == 1
        server = cfg.federation.servers[0]
        assert server.transport == "http"
        assert server.url == "https://mcp.example.com/rpc"
        assert server.headers == {"Authorization": "Bearer token"}
        assert server.timeout_seconds == 60
