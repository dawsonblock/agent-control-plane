"""Tests for v0.7.0 (Phase 4.1) memory prune — semantic memory GC.

Tests the prune_superseded_nodes function and the `acp memory prune` CLI
command. Since Graphiti/FalkorDB is an optional extra, tests that require
it are skipped when the memory extra is not installed.

The CLI command tests use mocking to avoid requiring a running FalkorDB
instance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from acp.cli import app

runner = CliRunner()

try:
    import graphiti_core  # noqa: F401

    GRAPHITI_INSTALLED = True
except ImportError:
    GRAPHITI_INSTALLED = False

memory_skip = pytest.mark.skipif(
    not GRAPHITI_INSTALLED,
    reason="memory extra not installed (uv sync --extra memory)",
)


# --------------------------------------------------------------------------- #
# CLI: acp memory prune — dry run mode (no FalkorDB required)
# --------------------------------------------------------------------------- #


def _make_repo_config(tmp_path: Path) -> Path:
    """Create a minimal repo.yaml for testing."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = tmp_path / "demo.repo.yaml"
    config_path.write_text(f"repo:\n  name: demo\n  path: {repo_path}\n")
    return config_path


def test_memory_prune_dry_run_no_nodes(tmp_path):
    """`acp memory prune --dry-run` reports zero nodes when graph is clean."""
    config = _make_repo_config(tmp_path)
    with patch(
        "acp.memory.graphiti_client.prune_superseded_nodes",
        return_value={
            "dry_run": True,
            "found": 0,
            "pruned": 0,
            "nodes": [],
            "older_than_days": 90,
        },
    ):
        result = runner.invoke(
            app,
            [
                "memory",
                "prune",
                "--config",
                str(config),
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "no superseded nodes found" in result.output


def test_memory_prune_dry_run_with_nodes(tmp_path):
    """`acp memory prune --dry-run` reports found nodes without deleting."""
    config = _make_repo_config(tmp_path)
    mock_nodes = [
        {"node_id": "node-abc", "superseded_at": "2025-01-01T00:00:00Z", "days_superseded": 120},
        {"node_id": "node-def", "superseded_at": "2025-02-01T00:00:00Z", "days_superseded": 89},
    ]
    with patch(
        "acp.memory.graphiti_client.prune_superseded_nodes",
        return_value={
            "dry_run": True,
            "found": 2,
            "pruned": 0,
            "nodes": mock_nodes,
            "older_than_days": 90,
        },
    ):
        result = runner.invoke(
            app,
            [
                "memory",
                "prune",
                "--config",
                str(config),
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "found 2 superseded node(s)" in result.output
    assert "node-abc" in result.output
    assert "Dry run" in result.output
    assert "no nodes deleted" in result.output


def test_memory_prune_no_dry_run(tmp_path):
    """`acp memory prune --no-dry-run` actually deletes nodes."""
    config = _make_repo_config(tmp_path)
    mock_nodes = [
        {"node_id": "node-abc", "superseded_at": "2025-01-01T00:00:00Z", "days_superseded": 120},
    ]
    with patch(
        "acp.memory.graphiti_client.prune_superseded_nodes",
        return_value={
            "dry_run": False,
            "found": 1,
            "pruned": 1,
            "nodes": mock_nodes,
            "older_than_days": 90,
        },
    ):
        result = runner.invoke(
            app,
            [
                "memory",
                "prune",
                "--config",
                str(config),
                "--no-dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "pruned 1 node(s)" in result.output
    assert "Dry run" not in result.output


def test_memory_prune_custom_older_than_days(tmp_path):
    """`acp memory prune --older-than-days 30` passes the threshold through."""
    config = _make_repo_config(tmp_path)
    with patch(
        "acp.memory.graphiti_client.prune_superseded_nodes",
        return_value={
            "dry_run": True,
            "found": 0,
            "pruned": 0,
            "nodes": [],
            "older_than_days": 30,
        },
    ) as mock_prune:
        result = runner.invoke(
            app,
            [
                "memory",
                "prune",
                "--config",
                str(config),
                "--older-than-days",
                "30",
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "older_than_days=30" in result.output
    # Verify the threshold was passed to the function.
    mock_prune.assert_called_once()
    call_kwargs = mock_prune.call_args
    # The function is called with keyword args.
    assert call_kwargs.kwargs.get("older_than_days", 90) == 30


def test_memory_prune_truncates_long_list(tmp_path):
    """When there are >20 nodes, only the first 20 are shown."""
    config = _make_repo_config(tmp_path)
    mock_nodes = [
        {
            "node_id": f"node-{i}",
            "superseded_at": "2025-01-01T00:00:00Z",
            "days_superseded": 100 + i,
        }
        for i in range(25)
    ]
    with patch(
        "acp.memory.graphiti_client.prune_superseded_nodes",
        return_value={
            "dry_run": True,
            "found": 25,
            "pruned": 0,
            "nodes": mock_nodes,
            "older_than_days": 90,
        },
    ):
        result = runner.invoke(
            app,
            [
                "memory",
                "prune",
                "--config",
                str(config),
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "found 25 superseded node(s)" in result.output
    assert "and 5 more" in result.output


def test_memory_prune_config_not_found(tmp_path):
    """`acp memory prune` exits with error when config file is missing."""
    result = runner.invoke(
        app,
        [
            "memory",
            "prune",
            "--config",
            str(tmp_path / "nonexistent.yaml"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "config file not found" in result.output


def test_memory_prune_import_error(tmp_path):
    """`acp memory prune` handles ImportError when memory extra not installed."""
    config = _make_repo_config(tmp_path)
    with patch(
        "acp.memory.graphiti_client.prune_superseded_nodes",
        side_effect=ImportError("graphiti-core not installed"),
    ):
        result = runner.invoke(
            app,
            [
                "memory",
                "prune",
                "--config",
                str(config),
                "--dry-run",
            ],
        )
    assert result.exit_code == 1, result.output
    assert "graphiti-core not installed" in result.output


# --------------------------------------------------------------------------- #
# prune_superseded_nodes — unit tests with mocked Graphiti client
# --------------------------------------------------------------------------- #


async def test_prune_dry_run_returns_found_count():
    """prune_superseded_nodes in dry-run mode returns found nodes without deleting."""
    from acp.memory.graphiti_client import prune_superseded_nodes

    mock_nodes = [
        {"node_id": "node-1", "superseded_at": "2025-01-01T00:00:00Z", "days_superseded": 120},
        {"node_id": "node-2", "superseded_at": "2025-02-01T00:00:00Z", "days_superseded": 100},
    ]
    with patch(
        "acp.memory.graphiti_client.find_superseded_nodes",
        new_callable=AsyncMock,
        return_value=mock_nodes,
    ):
        result = await prune_superseded_nodes(dry_run=True, older_than_days=90)

    assert result["dry_run"] is True
    assert result["found"] == 2
    assert result["pruned"] == 0  # dry run — nothing deleted
    assert len(result["nodes"]) == 2
    assert result["older_than_days"] == 90


async def test_prune_no_dry_run_deletes_nodes():
    """prune_superseded_nodes with dry_run=False attempts deletion."""
    from acp.memory.graphiti_client import prune_superseded_nodes

    mock_nodes = [
        {"node_id": "node-1", "superseded_at": "2025-01-01T00:00:00Z", "days_superseded": 120},
    ]
    mock_client = MagicMock()
    mock_client.driver = None  # no driver → returns 0 pruned
    mock_client.close = AsyncMock()
    with (
        patch(
            "acp.memory.graphiti_client.find_superseded_nodes",
            new_callable=AsyncMock,
            return_value=mock_nodes,
        ),
        patch(
            "acp.memory.graphiti_client._get_graphiti_client",
            return_value=mock_client,
        ),
    ):
        result = await prune_superseded_nodes(dry_run=False, older_than_days=90)

    assert result["dry_run"] is False
    assert result["found"] == 1
    # With no driver, pruned is 0 but the function doesn't raise.
    assert result["pruned"] == 0


async def test_prune_empty_graph():
    """prune_superseded_nodes with no superseded nodes returns zeros."""
    from acp.memory.graphiti_client import prune_superseded_nodes

    with patch(
        "acp.memory.graphiti_client.find_superseded_nodes",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await prune_superseded_nodes(dry_run=False, older_than_days=90)

    assert result["found"] == 0
    assert result["pruned"] == 0
    assert result["nodes"] == []
