"""Tests for v0.7.0 (Phase 2.2) — egress proxy logging and enforcement.

Tests the egress proxy feature:

  1. EgressLogger — collecting egress events, writing network_egress.json
  2. Egress analysis — comparing accessed domains against the allowlist
  3. Artifact format — network_egress.json structure and content
  4. Review gate integration — has_egress_violations helper
  5. Config — ProxySection defaults and validation
"""

from __future__ import annotations

import json

import pytest

from acp.config import ProxySection
from acp.egress import (
    EgressLogger,
    analyze_egress_log,
    has_egress_violations,
)


# --------------------------------------------------------------------------- #
# 1. EgressLogger — collecting events
# --------------------------------------------------------------------------- #


def test_egress_logger_empty():
    """A fresh EgressLogger has no events."""
    logger = EgressLogger()
    assert logger.events == []
    assert logger.domains == set()


def test_egress_logger_records_requests():
    """log_request records egress events."""
    logger = EgressLogger()
    logger.log_request("pypi.org", method="GET", path="/simple", status_code=200)
    logger.log_request("github.com", method="POST", path="/api", status_code=201)

    assert len(logger.events) == 2
    assert logger.events[0].domain == "pypi.org"
    assert logger.events[0].method == "GET"
    assert logger.events[1].domain == "github.com"
    assert logger.events[1].status_code == 201


def test_egress_logger_domains():
    """domains property returns unique set of accessed domains."""
    logger = EgressLogger()
    logger.log_request("pypi.org")
    logger.log_request("pypi.org")
    logger.log_request("github.com")

    assert logger.domains == {"pypi.org", "github.com"}


def test_egress_logger_normalizes_case():
    """Domains are lowercased for consistent comparison."""
    logger = EgressLogger()
    logger.log_request("PyPI.org")
    logger.log_request("pypi.org")

    assert logger.domains == {"pypi.org"}


def test_egress_logger_blocked_flag():
    """log_request can mark a request as blocked."""
    logger = EgressLogger()
    logger.log_request("evil.com", blocked=True)

    assert logger.events[0].blocked is True


# --------------------------------------------------------------------------- #
# 2. Egress analysis — allowlist enforcement
# --------------------------------------------------------------------------- #


def test_analyze_no_violations():
    """All accessed domains are in the allowlist → no violations."""
    logger = EgressLogger()
    logger.log_request("pypi.org")
    logger.log_request("github.com")

    violations = logger.analyze(allowed_domains=["pypi.org", "github.com"])
    assert len(violations) == 0


def test_analyze_finds_violations():
    """Domains not in the allowlist are flagged as violations."""
    logger = EgressLogger()
    logger.log_request("pypi.org")
    logger.log_request("evil.com")
    logger.log_request("evil.com")

    violations = logger.analyze(allowed_domains=["pypi.org"])
    assert len(violations) == 1
    assert violations[0].domain == "evil.com"
    assert violations[0].request_count == 2
    assert violations[0].allowed is False


def test_analyze_case_insensitive():
    """Allowlist matching is case-insensitive."""
    logger = EgressLogger()
    logger.log_request("PyPI.org")

    violations = logger.analyze(allowed_domains=["pypi.org"])
    assert len(violations) == 0


def test_analyze_empty_allowlist():
    """Empty allowlist → all domains are violations."""
    logger = EgressLogger()
    logger.log_request("pypi.org")

    violations = logger.analyze(allowed_domains=[])
    assert len(violations) == 1
    assert violations[0].domain == "pypi.org"


def test_analyze_empty_log():
    """No egress events → no violations."""
    logger = EgressLogger()
    violations = logger.analyze(allowed_domains=["pypi.org"])
    assert len(violations) == 0


# --------------------------------------------------------------------------- #
# 3. Artifact format — network_egress.json
# --------------------------------------------------------------------------- #


def test_write_artifact(tmp_path):
    """write_artifact produces a valid network_egress.json file."""
    logger = EgressLogger()
    logger.log_request("pypi.org", method="GET", status_code=200)
    logger.log_request("evil.com", method="POST", status_code=200)

    artifacts_dir = tmp_path / "artifacts"
    artifact_path = logger.write_artifact(
        artifacts_dir,
        allowed_domains=["pypi.org"],
    )

    assert artifact_path.is_file()
    assert artifact_path.name == "network_egress.json"

    data = json.loads(artifact_path.read_text())
    assert "events" in data
    assert len(data["events"]) == 2
    assert "domains" in data
    assert "pypi.org" in data["domains"]
    assert "evil.com" in data["domains"]
    assert "allowed_domains" in data
    assert "pypi.org" in data["allowed_domains"]
    assert "violations" in data
    assert len(data["violations"]) == 1
    assert data["violations"][0]["domain"] == "evil.com"
    assert "summary" in data
    assert data["summary"]["total_requests"] == 2
    assert data["summary"]["unique_domains"] == 2
    assert data["summary"]["violation_count"] == 1


def test_write_artifact_custom_filename(tmp_path):
    """write_artifact respects the log_filename parameter."""
    logger = EgressLogger()
    logger.log_request("example.com")

    artifact_path = logger.write_artifact(
        tmp_path / "artifacts",
        log_filename="custom_egress.json",
    )
    assert artifact_path.name == "custom_egress.json"


def test_write_artifact_creates_directory(tmp_path):
    """write_artifact creates the artifacts directory if it doesn't exist."""
    logger = EgressLogger()
    logger.log_request("example.com")

    artifacts_dir = tmp_path / "nested" / "artifacts"
    artifact_path = logger.write_artifact(artifacts_dir)
    assert artifact_path.is_file()
    assert artifacts_dir.is_dir()


# --------------------------------------------------------------------------- #
# 4. Review gate integration
# --------------------------------------------------------------------------- #


def test_has_egress_violations_true(tmp_path):
    """has_egress_violations returns True when violations exist."""
    logger = EgressLogger()
    logger.log_request("evil.com")
    logger.write_artifact(tmp_path, allowed_domains=[])

    assert has_egress_violations(tmp_path) is True


def test_has_egress_violations_false_no_violations(tmp_path):
    """has_egress_violations returns False when no violations."""
    logger = EgressLogger()
    logger.log_request("pypi.org")
    logger.write_artifact(tmp_path, allowed_domains=["pypi.org"])

    assert has_egress_violations(tmp_path) is False


def test_has_egress_violations_false_no_artifact(tmp_path):
    """has_egress_violations returns False when no artifact exists."""
    assert has_egress_violations(tmp_path) is False


def test_analyze_egress_log_from_file(tmp_path):
    """analyze_egress_log reads violations from a written artifact."""
    logger = EgressLogger()
    logger.log_request("pypi.org")
    logger.log_request("evil.com")
    logger.log_request("evil.com")

    artifact_path = logger.write_artifact(tmp_path, allowed_domains=["pypi.org"])
    violations = analyze_egress_log(artifact_path, ["pypi.org"])

    assert len(violations) == 1
    assert violations[0].domain == "evil.com"
    assert violations[0].request_count == 2


def test_analyze_egress_log_missing_file(tmp_path):
    """analyze_egress_log returns empty list when file doesn't exist."""
    violations = analyze_egress_log(tmp_path / "nonexistent.json", [])
    assert violations == []


# --------------------------------------------------------------------------- #
# 5. Config — ProxySection
# --------------------------------------------------------------------------- #


def test_proxy_section_defaults():
    """ProxySection defaults to disabled."""
    section = ProxySection()
    assert not section.enabled
    assert section.proxy_port == 8080
    assert section.allowed_domains == []
    assert section.log_artifact == "network_egress.json"


def test_proxy_section_enabled_with_domains():
    """ProxySection accepts enabled state with allowed domains."""
    section = ProxySection(
        enabled=True,
        allowed_domains=["pypi.org", "github.com", "registry.npmjs.org"],
    )
    assert section.enabled
    assert len(section.allowed_domains) == 3


def test_proxy_section_rejects_invalid_port():
    """ProxySection rejects ports outside 1-65535."""
    with pytest.raises(ValueError, match="proxy_port"):
        ProxySection(proxy_port=0)
    with pytest.raises(ValueError, match="proxy_port"):
        ProxySection(proxy_port=70000)


def test_proxy_section_in_yaml(tmp_path):
    """Repo config loads proxy settings from YAML."""
    import yaml
    config_file = tmp_path / "test.repo.yaml"
    config_file.write_text(yaml.dump({
        "repo": {"name": "test", "path": str(tmp_path)},
        "proxy": {
            "enabled": True,
            "proxy_port": 9090,
            "allowed_domains": ["pypi.org", "github.com"],
        },
    }))
    from acp.config import load_repo_config
    cfg = load_repo_config(config_file)
    assert cfg.proxy.enabled
    assert cfg.proxy.proxy_port == 9090
    assert "pypi.org" in cfg.proxy.allowed_domains
