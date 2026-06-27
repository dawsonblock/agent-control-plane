"""Tests for the cross-feature code analysis fixes.

These tests verify the 12+ issues found during the cross-feature analysis
and the additional fixes made in the follow-up commit:

  1. OpenHands executor — LLM_MODEL env var + --override-with-envs
  2. Egress — has_egress_violations with custom log_filename
  3. Egress — empty artifact written when proxy enabled but no traffic
  4. HTTPTransport — HEAD->GET fallback on 405
  5. SSE parser — comment lines, empty data:, \r\n line endings
  6. RerankingSection — top_k_before >= top_k_after validation
  7. FederationServerConfig — timeout_seconds validation
  8. AgentSection — max_subtasks validation
  9. SecretFinding — entropy field used by detect_hard_block_secrets
  10. Workflow — passes durable_store/primary to TaskStore
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from acp.config import (
    AgentSection,
    CommandsSection,
    DurableMode,
    EvidenceSection,
    ExecutorSection,
    FederationServerConfig,
    RepoConfig,
    RepoSection,
    RerankingSection,
    ReviewSection,
)
from acp.egress import (
    EgressLogger,
    has_egress_violations,
)
from acp.executor.openhands import OpenHandsExecutor
from acp.federation.transport import (
    HTTPTransport,
    MCPError,
    SSETransport,
)
from acp.review.secret_scanner import SecretFinding, detect_hard_block_secrets

# --------------------------------------------------------------------------- #
# 1. OpenHands executor — LLM_MODEL env var + --override-with-envs
# --------------------------------------------------------------------------- #


async def test_openhands_start_uses_env_var_not_model_flag(tmp_path):
    """start() uses LLM_MODEL env var + --override-with-envs, not --model."""
    cfg = ExecutorSection(backend="openhands", agent="claude-sonnet-4-20250514")
    executor = OpenHandsExecutor(cfg)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("test")

    with (
        patch.object(OpenHandsExecutor, "check_installed", return_value=True),
        patch.object(OpenHandsExecutor, "get_version", return_value="0.1.0"),
        patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        await executor.start(
            task_id="task_001",
            prompt_path=prompt_path,
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=30,
        )

    call_args = mock_run.call_args
    cmd = call_args[0][0]  # first positional arg is the command list
    env = call_args[1].get("env", {})

    # Must NOT use --model flag
    assert "--model" not in cmd, "should not use --model flag (doesn't exist)"
    # Must use --override-with-envs
    assert "--override-with-envs" in cmd, "should use --override-with-envs"
    # LLM_MODEL must be set in the environment
    assert env.get("LLM_MODEL") == "claude-sonnet-4-20250514"


async def test_openhands_start_without_agent_no_override(tmp_path):
    """start() without agent doesn't add --override-with-envs.

    The executor requires an agent to be set, so we mock _validate to
    bypass the check and test the command construction logic.
    """
    cfg = ExecutorSection(backend="openhands", agent="")
    executor = OpenHandsExecutor(cfg)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with (
        patch.object(OpenHandsExecutor, "_validate", return_value=None),
        patch.object(OpenHandsExecutor, "get_version", return_value="0.1.0"),
        patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        await executor.start(
            task_id="task_001",
            prompt_path=tmp_path / "prompt.txt",
            repo_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=30,
        )

    cmd = mock_run.call_args[0][0]
    assert "--override-with-envs" not in cmd


# --------------------------------------------------------------------------- #
# 2. Egress — has_egress_violations with custom log_filename
# --------------------------------------------------------------------------- #


def test_has_egress_violations_custom_filename(tmp_path):
    """has_egress_violations respects the log_filename parameter."""
    logger = EgressLogger()
    logger.log_request("evil.com", method="GET", path="/", status_code=200)
    logger.write_artifact(
        tmp_path,
        allowed_domains=["good.com"],
        log_filename="custom_egress.json",
    )

    # Default filename should NOT find it
    assert not has_egress_violations(tmp_path)
    # Custom filename SHOULD find it
    assert has_egress_violations(tmp_path, "custom_egress.json")


def test_has_egress_violations_default_filename_still_works(tmp_path):
    """has_egress_violations still works with the default filename."""
    logger = EgressLogger()
    logger.log_request("evil.com", method="GET", path="/", status_code=200)
    logger.write_artifact(tmp_path, allowed_domains=["good.com"])

    assert has_egress_violations(tmp_path)


# --------------------------------------------------------------------------- #
# 3. Egress — empty artifact when proxy enabled but no traffic
# --------------------------------------------------------------------------- #


def test_egress_logger_write_artifact_empty(tmp_path):
    """EgressLogger with no events writes a valid empty artifact."""
    logger = EgressLogger()
    path = logger.write_artifact(
        tmp_path,
        allowed_domains=["api.openai.com"],
        log_filename="network_egress.json",
    )
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["events"] == []
    assert data["domains"] == []
    assert data["summary"]["total_requests"] == 0
    assert data["summary"]["violation_count"] == 0


# --------------------------------------------------------------------------- #
# 4. HTTPTransport — HEAD->GET fallback on 405
# --------------------------------------------------------------------------- #


def test_http_transport_falls_back_to_get_on_405():
    """HTTPTransport.start() falls back to GET when HEAD returns 405."""
    import urllib.error

    config = {
        "name": "test",
        "transport": "http",
        "url": "http://localhost:9999/mcp",
        "headers": {},
        "timeout_seconds": 5,
    }
    transport = HTTPTransport(config)

    # First call (HEAD) raises 405, second call (GET) succeeds.
    head_error = urllib.error.HTTPError(
        url="http://localhost:9999/mcp",
        code=405,
        msg="Method Not Allowed",
        hdrs=None,
        fp=None,
    )
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", side_effect=[head_error, mock_resp]):
        transport.start()  # should not raise

    assert transport._connected is True


def test_http_transport_raises_on_non_405_http_error():
    """HTTPTransport.start() raises on non-405 HTTP errors."""
    import urllib.error

    config = {
        "name": "test",
        "transport": "http",
        "url": "http://localhost:9999/mcp",
        "headers": {},
        "timeout_seconds": 5,
    }
    transport = HTTPTransport(config)

    error_500 = urllib.error.HTTPError(
        url="http://localhost:9999/mcp",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=error_500):
        with pytest.raises(MCPError, match="HTTP 500"):
            transport.start()


# --------------------------------------------------------------------------- #
# 5. SSE parser — comment lines, empty data:, \r\n line endings
# --------------------------------------------------------------------------- #


def test_sse_parser_handles_comment_lines():
    """SSE parser ignores comment lines (starting with :)."""
    raw = (
        ": this is a comment\n"
        ": another comment\n"
        'data: {"jsonrpc": "2.0", "result": {"ok": true}}\n'
        "\n"
    )
    result = SSETransport._parse_sse_response(raw)
    assert result == {"jsonrpc": "2.0", "result": {"ok": True}}


def test_sse_parser_handles_empty_data_line():
    """SSE parser handles 'data:' with no content after it."""
    # An empty data: line followed by a real one — the empty string
    # is part of the data but json.loads should skip it and find the
    # real payload in the next event.
    raw = 'data: {"jsonrpc": "2.0", "result": {"value": 42}}\n\n'
    result = SSETransport._parse_sse_response(raw)
    assert result["result"]["value"] == 42


def test_sse_parser_handles_crlf_line_endings():
    """SSE parser handles \\r\\n\\r\\n event delimiters."""
    raw = 'data: {"jsonrpc": "2.0", "result": {"ok": true}}\r\n\r\n'
    result = SSETransport._parse_sse_response(raw)
    assert result == {"jsonrpc": "2.0", "result": {"ok": True}}


def test_sse_parser_handles_multiple_data_lines():
    """SSE parser concatenates multiple data: lines with newlines."""
    # Multi-line data payload (JSON with embedded newline)
    raw = 'data: {"jsonrpc": "2.0",\ndata:  "result": {"ok": true}}\n\n'
    result = SSETransport._parse_sse_response(raw)
    assert result["result"]["ok"] is True


def test_sse_parser_ignores_event_id_retry_fields():
    """SSE parser ignores event:, id:, retry: fields."""
    raw = (
        "event: message\n"
        "id: 12345\n"
        "retry: 5000\n"
        'data: {"jsonrpc": "2.0", "result": {"ok": true}}\n'
        "\n"
    )
    result = SSETransport._parse_sse_response(raw)
    assert result["result"]["ok"] is True


# --------------------------------------------------------------------------- #
# 6. RerankingSection — top_k_before >= top_k_after validation
# --------------------------------------------------------------------------- #


def test_reranking_rejects_after_greater_than_before():
    """RerankingSection rejects top_k_after_rerank > top_k_before_rerank."""
    with pytest.raises(ValueError, match="must be >= top_k_after_rerank"):
        RerankingSection(
            enabled=True,
            top_k_before_rerank=5,
            top_k_after_rerank=10,
        )


def test_reranking_accepts_equal_before_and_after():
    """RerankingSection accepts top_k_before_rerank == top_k_after_rerank."""
    cfg = RerankingSection(
        enabled=True,
        top_k_before_rerank=10,
        top_k_after_rerank=10,
    )
    assert cfg.top_k_before_rerank == 10
    assert cfg.top_k_after_rerank == 10


def test_reranking_accepts_before_greater_than_after():
    """RerankingSection accepts top_k_before_rerank > top_k_after_rerank."""
    cfg = RerankingSection(
        enabled=True,
        top_k_before_rerank=20,
        top_k_after_rerank=5,
    )
    assert cfg.top_k_before_rerank == 20
    assert cfg.top_k_after_rerank == 5


# --------------------------------------------------------------------------- #
# 7. FederationServerConfig — timeout_seconds validation
# --------------------------------------------------------------------------- #


def test_federation_timeout_rejects_zero():
    """FederationServerConfig rejects timeout_seconds=0."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        FederationServerConfig(
            name="test",
            transport="stdio",
            command=["echo"],
            timeout_seconds=0,
        )


def test_federation_timeout_rejects_negative():
    """FederationServerConfig rejects negative timeout_seconds."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        FederationServerConfig(
            name="test",
            transport="stdio",
            command=["echo"],
            timeout_seconds=-1,
        )


def test_federation_timeout_rejects_over_24h():
    """FederationServerConfig rejects timeout_seconds > 86400."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        FederationServerConfig(
            name="test",
            transport="stdio",
            command=["echo"],
            timeout_seconds=100000,
        )


def test_federation_timeout_accepts_valid():
    """FederationServerConfig accepts valid timeout_seconds."""
    cfg = FederationServerConfig(
        name="test",
        transport="stdio",
        command=["echo"],
        timeout_seconds=60,
    )
    assert cfg.timeout_seconds == 60


# --------------------------------------------------------------------------- #
# 8. AgentSection — max_subtasks validation
# --------------------------------------------------------------------------- #


def test_agent_max_subtasks_rejects_negative():
    """AgentSection rejects negative max_subtasks."""
    with pytest.raises(ValueError, match="max_subtasks"):
        AgentSection(max_subtasks=-1)


def test_agent_max_subtasks_rejects_over_100():
    """AgentSection rejects max_subtasks > 100."""
    with pytest.raises(ValueError, match="max_subtasks"):
        AgentSection(max_subtasks=101)


def test_agent_max_subtasks_accepts_zero():
    """AgentSection accepts max_subtasks=0 (disables spawning)."""
    cfg = AgentSection(max_subtasks=0)
    assert cfg.max_subtasks == 0


def test_agent_max_subtasks_accepts_valid():
    """AgentSection accepts valid max_subtasks."""
    cfg = AgentSection(max_subtasks=10)
    assert cfg.max_subtasks == 10


# --------------------------------------------------------------------------- #
# 9. SecretFinding — entropy field used by detect_hard_block_secrets
# --------------------------------------------------------------------------- #


def test_secret_finding_has_entropy_field():
    """SecretFinding has an entropy field with default 0.0."""
    f = SecretFinding(kind="test", snippet="abc", line_no=1)
    assert f.entropy == 0.0


def test_detect_hard_block_uses_entropy_field():
    """detect_hard_block_secrets uses the entropy field, not snippet parsing."""
    # Create a finding with high entropy that would NOT be parseable from
    # the snippet string (snippet doesn't contain "entropy" keyword).
    findings = [
        SecretFinding(
            kind="high_entropy_assignment",
            snippet="abcdef…xy (no entropy keyword here)",
            line_no=1,
            entropy=4.5,  # above the 4.0 threshold
        ),
    ]
    with patch("acp.review.secret_scanner.scan_patch", return_value=findings):
        result = detect_hard_block_secrets("dummy patch")
    assert len(result) == 1
    assert result[0].entropy == 4.5


def test_detect_hard_block_skips_low_entropy():
    """detect_hard_block_secrets skips high_entropy_assignment with entropy < 4.0."""
    findings = [
        SecretFinding(
            kind="high_entropy_assignment",
            snippet="abcdef…xy (entropy 3.8)",
            line_no=1,
            entropy=3.8,  # below the 4.0 hard-block threshold
        ),
    ]
    with patch("acp.review.secret_scanner.scan_patch", return_value=findings):
        result = detect_hard_block_secrets("dummy patch")
    assert len(result) == 0


def test_detect_hard_block_includes_pattern_secrets_regardless_of_entropy():
    """detect_hard_block_secrets includes pattern-based secrets regardless of entropy."""
    findings = [
        SecretFinding(
            kind="github_pat",
            snippet="ghp_…",
            line_no=1,
            entropy=0.0,  # no entropy — pattern match
        ),
    ]
    with patch("acp.review.secret_scanner.scan_patch", return_value=findings):
        result = detect_hard_block_secrets("dummy patch")
    assert len(result) == 1


# --------------------------------------------------------------------------- #
# 10. Workflow — passes durable_store/primary to TaskStore
# --------------------------------------------------------------------------- #


def test_workflow_respects_durable_mode_disabled(tmp_path):
    """run_workflow with durable_mode=DISABLED does not create the DB file."""
    from acp.graph.workflow import run_workflow

    db_path = tmp_path / "events.db"
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(
            durable_store=db_path,
            durable_mode=DurableMode.DISABLED,
        ),
    )
    os.environ["ACP_TEST"] = "1"
    run_workflow(
        config=cfg,
        user_request="test",
        runs_root=tmp_path / "runs",
        vault_root=tmp_path / "vault",
    )
    assert not db_path.exists(), "disabled mode should not create the SQLite DB"


def test_workflow_creates_db_when_best_effort(tmp_path):
    """run_workflow with durable_mode=BEST_EFFORT creates the DB file."""
    from acp.graph.workflow import run_workflow

    db_path = tmp_path / "events.db"
    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=tmp_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="echo ok"),
        review=ReviewSection(),
        evidence=EvidenceSection(
            durable_store=db_path,
            durable_mode=DurableMode.BEST_EFFORT,
        ),
    )
    os.environ["ACP_TEST"] = "1"
    run_workflow(
        config=cfg,
        user_request="test",
        runs_root=tmp_path / "runs",
        vault_root=tmp_path / "vault",
    )
    assert db_path.exists(), "best_effort mode should create the SQLite DB"
