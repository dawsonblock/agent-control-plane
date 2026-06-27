"""v0.5.14 TruffleHog integration tests.

Tests the TruffleHog secret scanning integration:

  1. trufflehog_installed() returns False when not on PATH.
  2. scan_with_trufflehog returns empty list when not installed.
  3. scan_diff falls back to regex scanner when TruffleHog is not installed.
  4. scan_diff uses TruffleHog when installed (mocked).
  5. scan_diff merges findings from both scanners.
  6. Verified TruffleHog findings are included, unverified are excluded.
  7. ReviewSection has use_trufflehog config field.
  8. review_diff passes worktree_path to scan_diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from acp.config import ReviewSection
from acp.review.secret_scanner import (
    SecretFinding,
    scan_diff,
    scan_with_trufflehog,
    trufflehog_installed,
)

# --------------------------------------------------------------------------- #
# 1. trufflehog_installed()
# --------------------------------------------------------------------------- #


class TestTruffleHogInstalled:
    def test_returns_false_when_not_installed(self):
        with patch("acp.review.secret_scanner.shutil.which", return_value=None):
            assert trufflehog_installed() is False

    def test_returns_true_when_installed(self):
        with patch(
            "acp.review.secret_scanner.shutil.which", return_value="/usr/local/bin/trufflehog"
        ):
            assert trufflehog_installed() is True


# --------------------------------------------------------------------------- #
# 2. scan_with_trufflehog returns empty when not installed
# --------------------------------------------------------------------------- #


class TestScanWithTruffleHogNotInstalled:
    def test_returns_empty_when_not_installed(self, tmp_path):
        with patch("acp.review.secret_scanner.shutil.which", return_value=None):
            findings = scan_with_trufflehog(tmp_path)
            assert findings == []


# --------------------------------------------------------------------------- #
# 3. scan_diff falls back to regex scanner
# --------------------------------------------------------------------------- #


class TestScanDiffFallback:
    def test_falls_back_to_regex_when_trufflehog_not_installed(self):
        """scan_diff uses the regex scanner when TruffleHog is not available."""
        patch_text = """diff --git a/config.py b/config.py
+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
"""
        with patch("acp.review.secret_scanner.trufflehog_installed", return_value=False):
            findings, info = scan_diff(
                patch_text, worktree_path=Path("/tmp/fake"), use_trufflehog=True
            )
            # The regex scanner should catch the AWS key pattern.
            assert len(findings) > 0
            assert any(f.kind == "aws_access_key" for f in findings)
            # Degradation should be surfaced.
            assert info["degraded"] == "true"
            assert info["scanner"] == "regex_only"

    def test_use_trufflehog_false_skips_trufflehog(self):
        """scan_diff skips TruffleHog when use_trufflehog=False."""
        patch_text = '+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'
        with patch("acp.review.secret_scanner.trufflehog_installed", return_value=True):
            with patch("acp.review.secret_scanner.scan_with_trufflehog") as mock_th:
                findings, info = scan_diff(
                    patch_text, worktree_path=Path("/tmp/fake"), use_trufflehog=False
                )
                mock_th.assert_not_called()
                # Regex scanner still runs.
                assert len(findings) > 0
                assert info["scanner"] == "regex_only"
                assert info["degraded"] == "false"


# --------------------------------------------------------------------------- #
# 4. scan_diff uses TruffleHog when installed (mocked)
# --------------------------------------------------------------------------- #


class TestScanDiffWithTruffleHog:
    def test_uses_trufflehog_when_installed(self):
        """scan_diff calls TruffleHog when installed and use_trufflehog=True."""
        patch_text = "+some line"
        mock_findings = [SecretFinding(kind="trufflehog:aws", snippet="AKIA…LE", line_no=1)]

        with patch("acp.review.secret_scanner.trufflehog_installed", return_value=True):
            with patch(
                "acp.review.secret_scanner.scan_with_trufflehog", return_value=mock_findings
            ):
                findings, scan_info = scan_diff(
                    patch_text, worktree_path=Path("/tmp/fake"), use_trufflehog=True
                )
                # Should include the TruffleHog finding.
                assert any(f.kind == "trufflehog:aws" for f in findings)
                assert scan_info["scanner"] == "trufflehog+regex"


# --------------------------------------------------------------------------- #
# 5. scan_diff merges findings from both scanners
# --------------------------------------------------------------------------- #


class TestScanDiffMerges:
    def test_merges_findings_from_both_scanners(self):
        """scan_diff includes findings from both regex and TruffleHog."""
        patch_text = '+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'
        mock_th_findings = [SecretFinding(kind="trufflehog:openai", snippet="sk-…", line_no=5)]

        with patch("acp.review.secret_scanner.trufflehog_installed", return_value=True):
            with patch(
                "acp.review.secret_scanner.scan_with_trufflehog", return_value=mock_th_findings
            ):
                findings, scan_info = scan_diff(
                    patch_text, worktree_path=Path("/tmp/fake"), use_trufflehog=True
                )
                kinds = {f.kind for f in findings}
                # Regex scanner found the AWS key.
                assert "aws_access_key" in kinds
                # TruffleHog found the OpenAI key.
                assert "trufflehog:openai" in kinds

    def test_dedupes_findings_by_kind_and_line(self):
        """scan_diff doesn't duplicate findings with the same (kind, line_no)."""
        patch_text = "+some line"
        # TruffleHog finding with same kind+line as a regex finding.
        mock_th_findings = [SecretFinding(kind="aws_access_key", snippet="AKIA…LE", line_no=1)]

        with patch("acp.review.secret_scanner.trufflehog_installed", return_value=True):
            with patch(
                "acp.review.secret_scanner.scan_with_trufflehog", return_value=mock_th_findings
            ):
                with patch(
                    "acp.review.secret_scanner.scan_patch",
                    return_value=[
                        SecretFinding(kind="aws_access_key", snippet="AKIA…LE", line_no=1),
                    ],
                ):
                    findings, scan_info = scan_diff(
                        patch_text, worktree_path=Path("/tmp/fake"), use_trufflehog=True
                    )
                    # Should not duplicate.
                    aws_findings = [f for f in findings if f.kind == "aws_access_key"]
                    assert len(aws_findings) == 1


# --------------------------------------------------------------------------- #
# 6. Verified vs unverified TruffleHog findings
# --------------------------------------------------------------------------- #


class TestTruffleHogVerifiedFiltering:
    def test_only_verified_findings_included(self, tmp_path):
        """scan_with_trufflehog only includes verified findings."""
        # Mock TruffleHog JSON output with one verified and one unverified.
        th_output = (
            json.dumps(
                {
                    "DetectorName": "AWS",
                    "Verified": True,
                    "Raw": "AKIAIOSFODNN7EXAMPLE",
                    "SourceMetadata": {"Metadata": {"line": 10}},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "DetectorName": "GitHub",
                    "Verified": False,
                    "Raw": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
                    "SourceMetadata": {"Metadata": {"line": 20}},
                }
            )
        )

        mock_proc = MagicMock(stdout=th_output, stderr="", returncode=0)

        with patch(
            "acp.review.secret_scanner.shutil.which", return_value="/usr/local/bin/trufflehog"
        ):
            with patch("acp.review.secret_scanner.subprocess.run", return_value=mock_proc):
                findings = scan_with_trufflehog(tmp_path)
                # Only the verified AWS finding should be included.
                assert len(findings) == 1
                assert findings[0].kind == "trufflehog:aws"
                assert findings[0].line_no == 10

    def test_handles_malformed_json_gracefully(self, tmp_path):
        """scan_with_trufflehog skips malformed JSON lines."""
        th_output = "not json\n" + json.dumps(
            {
                "DetectorName": "AWS",
                "Verified": True,
                "Raw": "AKIAIOSFODNN7EXAMPLE",
            }
        )

        mock_proc = MagicMock(stdout=th_output, stderr="", returncode=0)

        with patch(
            "acp.review.secret_scanner.shutil.which", return_value="/usr/local/bin/trufflehog"
        ):
            with patch("acp.review.secret_scanner.subprocess.run", return_value=mock_proc):
                findings = scan_with_trufflehog(tmp_path)
                # Should skip the malformed line and include the valid one.
                assert len(findings) == 1
                assert findings[0].kind == "trufflehog:aws"

    def test_handles_timeout_gracefully(self, tmp_path):
        """scan_with_trufflehog returns empty list on timeout."""
        import subprocess

        with patch(
            "acp.review.secret_scanner.shutil.which", return_value="/usr/local/bin/trufflehog"
        ):
            with patch(
                "acp.review.secret_scanner.subprocess.run",
                side_effect=subprocess.TimeoutExpired("cmd", 10),
            ):
                findings = scan_with_trufflehog(tmp_path)
                assert findings == []


# --------------------------------------------------------------------------- #
# 7. ReviewSection has use_trufflehog config field
# --------------------------------------------------------------------------- #


class TestReviewSectionConfig:
    def test_use_trufflehog_defaults_to_true(self):
        rs = ReviewSection()
        assert rs.use_trufflehog is True

    def test_use_trufflehog_can_be_disabled(self):
        rs = ReviewSection(use_trufflehog=False)
        assert rs.use_trufflehog is False


# --------------------------------------------------------------------------- #
# 8. review_diff passes worktree_path to scan_diff
# --------------------------------------------------------------------------- #


class TestReviewDiffPassesWorktree:
    def test_review_diff_accepts_worktree_path(self, tmp_path):
        """review_diff accepts an optional worktree_path parameter."""
        from acp.config import CommandsSection, RepoConfig, RepoSection
        from acp.gitops.diff import DiffCapture
        from acp.review.diff_reviewer import review_diff

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            commands=CommandsSection(),
            review=ReviewSection(block_secret_leaks=True),
        )
        diff = DiffCapture(
            patch="+some harmless line\n",
            stat="1 file changed, 1 insertion(+)",
            changed_files=["test.py"],
            insertions=1,
            deletions=0,
        )

        # Should not raise even with worktree_path=None.
        review = review_diff(
            diff=diff,
            command_results=[],
            repo_config=cfg,
            artifacts_dir=tmp_path / "artifacts",
            worktree_path=None,
        )
        assert review is not None
