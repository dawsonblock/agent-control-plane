"""Tests for agent federation via MCP (v0.6.9).

Tests the MCP client, FederationManager, and config integration.
The MCP client tests use a mock MCP server (a Python script that
speaks JSON-RPC over stdio) to avoid requiring a real MCP server.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from acp.config import FederationSection, FederationServerConfig, RepoConfig, RepoSection
from acp.federation.client import (
    FederationManager,
    MCPClient,
    MCPError,
    MCPTool,
    MCPToolResult,
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
                {"name": "search", "description": "Search the codebase", "inputSchema": {"type": "object"}},
                {"name": "analyze", "description": "Analyze a file", "inputSchema": {"type": "object"}},
            ]}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            return {"content": [{"type": "text", "text": f"Result of {name} with {json.dumps(args)}"}]}
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
        client = MCPClient(
            name="test-server",
            command=[sys.executable, str(mock_server_path)],
            timeout_seconds=10,
        )
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
        client = MCPClient(
            name="test-server",
            command=[sys.executable, str(mock_server_path)],
            timeout_seconds=10,
        )
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
        client = MCPClient(
            name="bad-server",
            command=["nonexistent-binary-xyz"],
        )
        with pytest.raises(MCPError, match="command not found"):
            client.start()

    def test_stop_is_idempotent(self, mock_server_path: Path):
        """Calling stop() multiple times is safe."""
        client = MCPClient(
            name="test-server",
            command=[sys.executable, str(mock_server_path)],
        )
        client.start()
        client.stop()
        client.stop()  # should not raise

    def test_list_tools_graceful_degradation(self):
        """list_tools returns empty list on failure, not raise."""
        client = MCPClient(
            name="bad-server",
            command=["nonexistent-binary-xyz"],
        )
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
        self, mock_server_path: Path,
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
        cfg = FederationSection(servers=[
            FederationServerConfig(
                name="my-server",
                command=["python", "-m", "my_mcp_server"],
            ),
        ])
        assert len(cfg.servers) == 1
        assert cfg.servers[0].name == "my-server"
        assert cfg.servers[0].command == ["python", "-m", "my_mcp_server"]

    def test_federation_in_repo_config(self):
        cfg = RepoConfig(
            repo=RepoSection(name="test", path=Path("/tmp/test")),
            federation=FederationSection(servers=[
                FederationServerConfig(
                    name="search-agent",
                    command=["python", "-m", "search_mcp"],
                    env={"API_KEY": "secret"},
                    timeout_seconds=60,
                ),
            ]),
        )
        assert len(cfg.federation.servers) == 1
        assert cfg.federation.servers[0].name == "search-agent"
        assert cfg.federation.servers[0].env == {"API_KEY": "secret"}
        assert cfg.federation.servers[0].timeout_seconds == 60

    def test_federation_section_optional_in_yaml(self, tmp_path: Path):
        """A repo config without a federation section loads fine."""
        import yaml
        config_file = tmp_path / "test.repo.yaml"
        config_file.write_text(yaml.dump({
            "repo": {"name": "test", "path": str(tmp_path)},
        }))
        from acp.config import load_repo_config
        cfg = load_repo_config(config_file)
        assert cfg.federation.servers == []
