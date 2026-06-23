"""Unit tests for secret_scanner — the M5 hard-block trigger.

Tests that known credential patterns are detected and legitimate content
(placeholder values, code comments with SHAs) is not falsely flagged.
"""

from __future__ import annotations

from acp.review.secret_scanner import scan_patch


def test_aws_access_key_detected() -> None:
    # Construct at runtime to avoid GitHub secret scanning false positive.
    parts = ["AKIA", "IOSFODNN7EXAMPLE"]
    patch = "+export AWS_ACCESS_KEY_ID=" + "".join(parts) + "\n"
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "aws_access_key" in kinds


def test_aws_secret_detected() -> None:
    patch = '+export AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n'
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "aws_secret" in kinds


def test_github_pat_detected() -> None:
    # Construct at runtime to avoid GitHub secret scanning false positive.
    parts = ["ghp", "_abcdef12345678901234567890123456789012"]
    patch = "+GITHUB_TOKEN=" + "".join(parts) + "\n"
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "github_pat" in kinds


def test_openai_key_detected() -> None:
    patch = '+OPENAI_API_KEY="sk-proj-abcdef1234567890abcdef1234567890"\n'
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "openai_key" in kinds


def test_private_key_block_detected() -> None:
    patch = "+-----BEGIN RSA PRIVATE KEY-----\n"
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "private_key_block" in kinds


def test_jwt_detected() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNqP3kE4kUJ6Tq3yQw"
    patch = f"+Bearer {jwt}\n"
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "jwt" in kinds


def test_slack_token_detected() -> None:
    # Construct at runtime to avoid GitHub secret scanning false positive.
    parts = ["xoxb", "-123456789012-1234567890123-abc123def456"]
    patch = "+" + "".join(parts) + "\n"
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "slack_token" in kinds


def test_stripe_key_detected() -> None:
    # Build the key at runtime to avoid GitHub push protection flagging it.
    stripe_parts = ["sk", "_live_", "abcdefghijklmnopqrstuvwxyz012345"]
    patch = "+" + "".join(stripe_parts) + "\n"
    findings = scan_patch(patch)
    kinds = {f.kind for f in findings}
    assert "stripe_key" in kinds


def test_placeholder_not_detected() -> None:
    patch = '+API_KEY="YOUR_API_KEY_HERE"\n'
    findings = scan_patch(patch)
    assert len(findings) == 0, f"placeholder flagged as secret: {findings}"


def test_commit_sha_not_detected() -> None:
    """40-hex strings in comments should not trigger legacy token detection."""
    patch = "+# Commit: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n"
    findings = scan_patch(patch)
    gh_legacy = [f for f in findings if f.kind == "github_legacy_token"]
    assert len(gh_legacy) == 0, f"commit SHA flagged as secret: {gh_legacy}"


def test_deleted_lines_not_scanned() -> None:
    """Only added lines (starting with +) are scanned, not context or deletions."""
    # Construct at runtime to avoid GitHub secret scanning false positive.
    aws_parts = ["AKIA", "IOSFODNN7EXAMPLE"]
    patch = "-" + "".join(aws_parts) + """
 context line
+some normal code
"""
    findings = scan_patch(patch)
    assert len(findings) == 0


def test_empty_patch_no_findings() -> None:
    assert scan_patch("") == []


def test_high_entropy_assignment_captured() -> None:
    """Long assignment values with high entropy should be flagged."""
    patch = '+DB_PASSWORD="a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0"\n'
    findings = scan_patch(patch)
    assert len(findings) > 0