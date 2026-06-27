"""Egress proxy — logs all agent network traffic (Phase 2.2).

When ``proxy.enabled`` is True in the repo config, ACP runs a local
HTTP proxy that the agent's network traffic is routed through. The
proxy logs every external domain the agent accesses, producing a
``network_egress.json`` artifact in the run's ``artifacts/`` directory.

The review gate can then flag any domain not in ``proxy.allowed_domains``
as a policy violation — the agent accessed a network resource that
wasn't explicitly approved.

This module provides:

  - :class:`EgressLogger`: collects egress events (domain, timestamp,
    method, status_code) and writes them to ``network_egress.json``.
  - :func:`analyze_egress_log`: post-run analysis that compares the
    egress log against the allowed domains list and returns violations.
  - :func:`write_egress_artifact`: writes the egress log + analysis
    to the run's artifacts directory.

The actual MITM proxy (mitmproxy) is optional — when not installed,
the egress logger can still be used in test/mock mode by injecting
egress events directly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EgressEvent:
    """A single network egress event — one HTTP request to an external domain."""

    domain: str
    method: str = "GET"
    path: str = "/"
    status_code: int = 0
    timestamp: float = field(default_factory=time.time)
    blocked: bool = False  # True if the proxy blocked the request

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "timestamp": self.timestamp,
            "blocked": self.blocked,
        }


@dataclass
class EgressViolation:
    """A policy violation — a domain accessed that's not in the allowlist."""

    domain: str
    request_count: int
    allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "request_count": self.request_count,
            "allowed": self.allowed,
        }


class EgressLogger:
    """Collects egress events and writes the network_egress.json artifact.

    Usage::

        logger = EgressLogger()
        logger.log_request("pypi.org", method="GET", path="/simple", status_code=200)
        logger.log_request("evil.com", method="POST", path="/exfil", status_code=200)
        violations = logger.analyze(allowed_domains=["pypi.org", "github.com"])
        logger.write_artifact(artifacts_dir, violations=violations)
    """

    def __init__(self) -> None:
        self._events: list[EgressEvent] = []

    def log_request(
        self,
        domain: str,
        *,
        method: str = "GET",
        path: str = "/",
        status_code: int = 0,
        blocked: bool = False,
    ) -> None:
        """Record a single egress event.

        v0.7.4: Validates the domain format before recording. Malformed
        domains (empty, whitespace-only, or containing control characters)
        are rejected to prevent log poisoning and ensure the egress analysis
        is reliable.
        """
        # v0.7.4: Basic domain validation — reject empty/malformed domains
        # that could corrupt the egress analysis or be used for log poisoning.
        if not domain or not domain.strip():
            logger.warning("egress: rejecting empty domain in log_request")
            return
        domain_clean = domain.strip().lower()
        # Reject domains with control characters or null bytes.
        if any(ord(c) < 32 for c in domain_clean):
            logger.warning("egress: rejecting domain with control characters: %r", domain)
            return
        self._events.append(
            EgressEvent(
                domain=domain_clean,
                method=method,
                path=path,
                status_code=status_code,
                blocked=blocked,
            )
        )

    @property
    def events(self) -> list[EgressEvent]:
        """All recorded egress events."""
        return list(self._events)

    @property
    def domains(self) -> set[str]:
        """Set of all unique domains accessed."""
        return {e.domain for e in self._events}

    def analyze(
        self,
        allowed_domains: list[str],
    ) -> list[EgressViolation]:
        """Analyze the egress log against the allowed domains list.

        Returns a list of :class:`EgressViolation` for every domain
        that was accessed but is not in the allowlist. Domains in the
        allowlist are not included in the violations list.
        """
        allowed_set = {d.lower() for d in allowed_domains}
        # Count requests per domain.
        domain_counts: dict[str, int] = {}
        for e in self._events:
            domain_counts[e.domain] = domain_counts.get(e.domain, 0) + 1

        violations: list[EgressViolation] = []
        for domain, count in sorted(domain_counts.items()):
            is_allowed = domain in allowed_set
            if not is_allowed:
                violations.append(
                    EgressViolation(
                        domain=domain,
                        request_count=count,
                        allowed=False,
                    )
                )
        return violations

    def write_artifact(
        self,
        artifacts_dir: Path,
        *,
        allowed_domains: list[str] | None = None,
        violations: list[EgressViolation] | None = None,
        log_filename: str = "network_egress.json",
    ) -> Path:
        """Write the egress log + analysis to the artifacts directory.

        The artifact is a JSON file with:
          - ``events``: list of all egress events
          - ``domains``: list of unique domains accessed
          - ``allowed_domains``: the configured allowlist
          - ``violations``: list of policy violations (domains not in allowlist)
          - ``summary``: counts (total_requests, unique_domains, violation_count)

        Returns the path to the written artifact.
        """
        artifacts_dir = Path(artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        if violations is None and allowed_domains is not None:
            violations = self.analyze(allowed_domains)

        artifact = {
            "events": [e.to_dict() for e in self._events],
            "domains": sorted(self.domains),
            "allowed_domains": sorted(d.lower() for d in (allowed_domains or [])),
            "violations": [v.to_dict() for v in (violations or [])],
            "summary": {
                "total_requests": len(self._events),
                "unique_domains": len(self.domains),
                "violation_count": len(violations or []),
            },
        }

        artifact_path = artifacts_dir / log_filename
        artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True))
        return artifact_path


def analyze_egress_log(
    egress_log_path: Path,
    allowed_domains: list[str],
) -> list[EgressViolation]:
    """Analyze a written network_egress.json artifact against the allowlist.

    This is the post-run analysis used by the review gate. It reads the
    artifact written by :meth:`EgressLogger.write_artifact` and returns
    the violations list.

    Returns an empty list if the artifact doesn't exist (egress proxy
    was not enabled for this run).
    """
    egress_log_path = Path(egress_log_path)
    if not egress_log_path.is_file():
        return []

    try:
        data = json.loads(egress_log_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    violations_data = data.get("violations", [])
    return [
        EgressViolation(
            domain=v["domain"],
            request_count=v["request_count"],
            allowed=v.get("allowed", False),
        )
        for v in violations_data
    ]


def has_egress_violations(
    artifacts_dir: Path,
    log_filename: str = "network_egress.json",
) -> bool:
    """Check if a run's artifacts directory has egress violations.

    Used by the review gate to determine if the egress proxy detected
    any policy violations. Returns False if the egress log doesn't exist
    (proxy was not enabled) or if there are no violations.

    Args:
        artifacts_dir: The run's artifacts directory.
        log_filename: The egress log filename (default: "network_egress.json").
            Should match ``proxy.log_artifact`` in the repo config.
    """
    egress_log = Path(artifacts_dir) / log_filename
    if not egress_log.is_file():
        return False

    try:
        data = json.loads(egress_log.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    return bool(data.get("summary", {}).get("violation_count", 0) > 0)
