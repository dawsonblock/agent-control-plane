"""v0.5.15 Sandbox Evidence Semantics tests.

Tests the fixes for the Docker Sandboxes verifier bugs and event semantics:

  1. sandbox.stopped events don't break acp verify (post-run classification)
  2. sandbox.configured is written at validation, sandbox.started after launch
  3. sandbox.failed is written when sbx run fails
  4. network_policy rejects arbitrary strings (strict enum)
  5. network_policy is passed to sbx command (--network flag)
  6. non-main default branches work (master, develop)
  7. fake-sbx integration test with exact argv assertions
  8. artifact ignore policy is consistent (fast and deep verify agree)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from acp.config import ExecutorSection
from acp.errors import AgentConfigError
from acp.executor.sbx import SbxExecutor


def _executor_config(**kwargs) -> ExecutorSection:
    defaults = {
        "backend": "docker_sbx",
        "agent": "claude",
        "sandbox_name_prefix": "acp",
        "clone_mode": True,
        "network_policy": "locked_down",
        "remove_after_run": False,
    }
    defaults.update(kwargs)
    return ExecutorSection(**defaults)


def _mock_sbx_installed():
    return patch(
        "acp.executor.sbx.SbxExecutor.check_installed",
        return_value=True,
    )


# --------------------------------------------------------------------------- #
# 1. Verifier classifies sandbox events as post-run
# --------------------------------------------------------------------------- #


class TestSandboxEventVerifierClassification:
    """sandbox.stopped/started/configured/failed are post-run events."""

    def test_sandbox_stopped_does_not_break_verify(self, tmp_path):
        """sandbox.stopped after evidence.finalized must not break verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "test.txt").write_text("test artifact")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        events.write(EventType.AGENT_STARTED, {"agent": "test"})
        events.write(EventType.AGENT_FINISHED, {"agent": "test", "exit_code": 0})
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_path = run_dir / "artifacts" / "final_report.md"
        from acp.evidence.manifest import _sha256_file

        report_hash = _sha256_file(report_path)
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})
        # sandbox.stopped AFTER finalization — this used to break verify.
        events.write(EventType.SANDBOX_STOPPED, {"sandbox_name": "acp-test"})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        # This should NOT fail because sandbox.stopped is a post-run event.
        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True, "sandbox.stopped after finalization should not break verify"

    def test_sandbox_started_does_not_break_verify(self, tmp_path):
        """sandbox.started after evidence.finalized must not break verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "test.txt").write_text("test artifact")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_path = run_dir / "artifacts" / "final_report.md"
        from acp.evidence.manifest import _sha256_file

        report_hash = _sha256_file(report_path)
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})
        events.write(EventType.SANDBOX_STARTED, {"sandbox_name": "acp-test"})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True

    def test_sandbox_configured_does_not_break_verify(self, tmp_path):
        """A sandbox.configured event must not break verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "test.txt").write_text("test artifact")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": "acp-test",
                "sandbox_remote": "sandbox-acp-test",
                "executor": {
                    "backend": "docker_sbx",
                    "network_policy": "locked_down",
                    "clone_mode": True,
                    "agent": "claude",
                },
            },
        )
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_path = run_dir / "artifacts" / "final_report.md"
        from acp.evidence.manifest import _sha256_file

        report_hash = _sha256_file(report_path)
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True


# --------------------------------------------------------------------------- #
# 2. Network policy strict enum validation
# --------------------------------------------------------------------------- #


class TestNetworkPolicyEnum:
    def test_rejects_arbitrary_string(self):
        """network_policy='banana' is rejected at config validation time."""
        with pytest.raises(Exception, match="not valid"):
            _executor_config(network_policy="banana")

    def test_rejects_open(self):
        """network_policy='open' is rejected."""
        cfg = _executor_config(network_policy="open")
        executor = SbxExecutor(cfg)
        with (
            _mock_sbx_installed(),
            pytest.raises(
                AgentConfigError,
                match="network_policy='open'",
            ),
        ):
            executor._validate()  # pylint: disable=protected-access

    def test_accepts_locked_down(self):
        """network_policy='locked_down' is accepted."""
        cfg = _executor_config(network_policy="locked_down")
        executor = SbxExecutor(cfg)
        with (
            _mock_sbx_installed(),
            patch(
                "acp.executor.sbx.SbxExecutor.get_version",
                return_value="1.0",
            ),
        ):
            executor._validate()  # pylint: disable=protected-access

    def test_accepts_balanced(self):
        """network_policy='balanced' is accepted."""
        cfg = _executor_config(network_policy="balanced")
        executor = SbxExecutor(cfg)
        with (
            _mock_sbx_installed(),
            patch(
                "acp.executor.sbx.SbxExecutor.get_version",
                return_value="1.0",
            ),
        ):
            executor._validate()  # pylint: disable=protected-access


# --------------------------------------------------------------------------- #
# 3. Network policy passed to sbx command
# --------------------------------------------------------------------------- #


class TestNetworkPolicyPassedToSbx:
    def test_network_policy_in_command(self, tmp_path):
        """The sbx command includes --network <policy>."""
        cfg = _executor_config(network_policy="locked_down")
        executor = SbxExecutor(cfg)

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("test prompt")
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock_proc = MagicMock(returncode=0, stdout="ok", stderr="")
            return mock_proc

        with (
            _mock_sbx_installed(),
            patch(
                "acp.executor.sbx.SbxExecutor.get_version",
                return_value="1.0",
            ),
            patch(
                "acp.executor.sbx.subprocess.run",
                side_effect=fake_run,
            ),
        ):
            executor.start(
                task_id="task_20260101_0001",
                prompt_path=prompt_path,
                repo_path=tmp_path,
                artifact_dir=artifact_dir,
                timeout_seconds=30,
            )

        assert "--network" in captured_cmd
        network_idx = captured_cmd.index("--network")
        assert captured_cmd[network_idx + 1] == "locked_down"

    def test_network_policy_balanced_in_command(self, tmp_path):
        """The sbx command includes --network balanced when configured."""
        cfg = _executor_config(network_policy="balanced")
        executor = SbxExecutor(cfg)

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("test prompt")
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock_proc = MagicMock(returncode=0, stdout="ok", stderr="")
            return mock_proc

        with (
            _mock_sbx_installed(),
            patch(
                "acp.executor.sbx.SbxExecutor.get_version",
                return_value="1.0",
            ),
            patch(
                "acp.executor.sbx.subprocess.run",
                side_effect=fake_run,
            ),
        ):
            executor.start(
                task_id="task_20260101_0001",
                prompt_path=prompt_path,
                repo_path=tmp_path,
                artifact_dir=artifact_dir,
                timeout_seconds=30,
            )

        assert "--network" in captured_cmd
        network_idx = captured_cmd.index("--network")
        assert captured_cmd[network_idx + 1] == "balanced"


# --------------------------------------------------------------------------- #
# 4. Fake-sbx integration test with exact argv assertions
# --------------------------------------------------------------------------- #


class TestFakeSbxIntegration:
    """Integration test with a fake sbx binary that records argv."""

    def _create_fake_sbx(self, tmp_path: Path) -> Path:
        """Create fake sbx executable that records calls."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sbx_path = bin_dir / "sbx"

        # The fake sbx script records its argv to a file and exits 0.
        script = f'''#!/bin/sh
# Record the call to a log file.
echo "$@" >> "{tmp_path / "sbx_calls.log"}"
# Handle subcommands.
case "$1" in
    --version)
        echo "sbx version 1.0.0-fake"
        ;;
    run)
        # Simulate successful agent run.
        echo "agent completed successfully"
        ;;
    stop)
        echo "stopped"
        ;;
    rm)
        echo "removed"
        ;;
    *)
        echo "unknown command: $1" >&2
        exit 1
        ;;
esac
exit 0
'''
        sbx_path.write_text(script)
        sbx_path.chmod(0o755)
        return bin_dir

    def test_fake_sbx_run_command_argv(self, tmp_path):
        """The sbx run command has the correct argv."""
        bin_dir = self._create_fake_sbx(tmp_path)
        cfg = _executor_config(network_policy="locked_down")
        executor = SbxExecutor(cfg)

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("test prompt")
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        # Patch PATH to include our fake sbx.
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        with (
            patch.dict(os.environ, env),
            patch(
                "acp.executor.sbx.SbxExecutor.get_version",
                return_value="1.0.0-fake",
            ),
        ):
            result = executor.start(
                task_id="task_20260101_0001",
                prompt_path=prompt_path,
                repo_path=tmp_path,
                artifact_dir=artifact_dir,
                timeout_seconds=30,
            )

        assert result.exit_code == 0

        # Read the recorded calls.
        log_path = tmp_path / "sbx_calls.log"
        calls = log_path.read_text().strip().split("\n")
        # run --clone --name <name> --network <policy> <agent>
        run_call = [c for c in calls if c.startswith("run ")][0]
        parts = run_call.split()
        assert parts[0] == "run"
        assert "--clone" in parts
        assert "--name" in parts
        name_idx = parts.index("--name")
        assert parts[name_idx + 1] == "acp-task-20260101-0001"
        assert "--network" in parts
        network_idx = parts.index("--network")
        assert parts[network_idx + 1] == "locked_down"
        assert parts[-1] == "claude"

    def test_fake_sbx_stop_command(self, tmp_path):
        """The sbx stop command has the correct argv."""
        bin_dir = self._create_fake_sbx(tmp_path)
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-task"

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        with patch.dict(os.environ, env):
            result = executor.stop()

        assert result is True
        log_path = tmp_path / "sbx_calls.log"
        calls = log_path.read_text().strip().split("\n")
        stop_call = [c for c in calls if c.startswith("stop ")][0]
        parts = stop_call.split()
        assert parts[0] == "stop"
        assert parts[1] == "acp-test-task"

    def test_fake_sbx_remove_command(self, tmp_path):
        """The sbx rm command has the correct argv."""
        bin_dir = self._create_fake_sbx(tmp_path)
        cfg = _executor_config()
        executor = SbxExecutor(cfg)
        executor._sandbox_name = "acp-test-task"

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        with patch.dict(os.environ, env):
            result = executor.remove()

        assert result is True
        log_path = tmp_path / "sbx_calls.log"
        calls = log_path.read_text().strip().split("\n")
        rm_call = [c for c in calls if c.startswith("rm ")][0]
        parts = rm_call.split()
        assert parts[0] == "rm"
        assert parts[1] == "acp-test-task"


# --------------------------------------------------------------------------- #
# 5. Artifact ignore policy consistency
# --------------------------------------------------------------------------- #


class TestArtifactIgnoreConsistency:
    """Fast and deep verification agree on ignored files."""

    def test_pycache_not_in_manifest(self, tmp_path):
        """__pycache__/*.pyc tracked in generated_artifacts, not artifacts."""
        from acp.events import EventWriter
        from acp.evidence.manifest import build_evidence_manifest
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)

        # Create a real artifact and a generated artifact.
        (artifacts_dir / "diff.patch").write_text("diff content")
        pycache_dir = artifacts_dir / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "junk.pyc").write_text("compiled junk")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        artifact_keys = set(manifest.get("artifacts", {}).keys())
        generated_keys = set(manifest.get("generated_artifacts", {}).keys())

        # diff.patch should be in the main artifacts section.
        assert "artifacts/diff.patch" in artifact_keys
        # __pycache__/junk.pyc should NOT be in the main artifacts section.
        assert not any("__pycache__" in k for k in artifact_keys)
        assert not any(".pyc" in k for k in artifact_keys)
        # v0.7.4: But it SHOULD be in generated_artifacts — tracked as
        # evidence to close the __pycache__ evasion loophole.
        assert any("__pycache__" in k for k in generated_keys), (
            "generated_artifacts should contain __pycache__ files"
        )
        assert any(".pyc" in k for k in generated_keys), (
            "generated_artifacts should contain .pyc files"
        )

    def test_fast_and_deep_agree_on_ignored(self, tmp_path):
        """Fast and deep verification both pass when a .pyc file changes."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)

        (artifacts_dir / "diff.patch").write_text("diff content")
        pycache_dir = artifacts_dir / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "junk.pyc").write_text("original junk")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        # Compute the real artifact hash so evidence.finalized matches.
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(
            EventType.EVIDENCE_FINALIZED,
            {
                "artifact_content_hash": real_hash,
            },
        )

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        # v0.7.4: __pycache__/*.pyc files are now tracked as
        # generated_artifacts in the manifest. Changing them DOES break
        # both fast and deep verify (this is the security fix — previously
        # a malicious agent could modify files in __pycache__ undetected).
        # The artifact_content_hash now covers generated files too, so
        # fast verify detects the change via the evidence.finalized event.
        (pycache_dir / "junk.pyc").write_text("modified junk")

        fast_result = verify_evidence_manifest(run_dir, deep=False)
        deep_result = verify_evidence_manifest(run_dir, deep=True)

        # Both fast and deep verify FAIL — the .pyc hash changed after the
        # manifest was built. This is the intended behavior: generated files
        # are now evidence, closing the __pycache__ evasion loophole.
        assert fast_result is False, (
            "fast verify should detect .pyc modification (generated files are evidence)"
        )
        assert deep_result is False, (
            "deep verify should detect .pyc modification (generated files are evidence)"
        )


# --------------------------------------------------------------------------- #
# 6. Non-main branch support
# --------------------------------------------------------------------------- #


class TestNonMainBranchSupport:
    """SbxExecutor uses cfg.repo.default_branch, not hardcoded 'main'."""

    def test_capture_diff_uses_default_branch(self, tmp_path):
        """capture_diff_from_remote works with non-main default branches."""
        from git import Repo

        from acp.gitops.diff import capture_diff_from_remote

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        repo = Repo.init(str(repo_path))
        repo.git.config("user.email", "test@acp.local")
        repo.git.config("user.name", "ACP Test")
        (repo_path / "README.md").write_text("# base\n")
        repo.git.add(".")
        repo.git.commit("-m", "base commit")
        repo.git.branch("-M", "master")  # Use master, not main.

        remote_path = tmp_path / "sandbox-remote"
        Repo.init(str(remote_path), bare=True)
        repo.create_remote("sandbox-acp-test", str(remote_path))

        repo.git.checkout("-b", "sandbox-work")
        (repo_path / "NEW_FILE.md").write_text("# agent created this\n")
        repo.git.add(".")
        repo.git.commit("-m", "agent change")
        repo.git.push("sandbox-acp-test", "sandbox-work:refs/heads/master")

        repo.git.checkout("master")
        repo.git.fetch("sandbox-acp-test")

        artifacts = tmp_path / "artifacts"
        diff = capture_diff_from_remote(
            repo_path=repo_path,
            remote="sandbox-acp-test",
            base_branch="master",
            artifacts_dir=artifacts,
            remote_branch="master",
        )

        assert "NEW_FILE.md" in diff.changed_files
        assert diff.insertions > 0


# --------------------------------------------------------------------------- #
# 7. Persisted DigestCache
# --------------------------------------------------------------------------- #


class TestPersistedDigestCache:
    """DigestCache can be saved to and loaded from disk."""

    def test_save_and_load_roundtrip(self, tmp_path):
        """DigestCache.save_to + load_from roundtrips correctly."""
        from acp.evidence.manifest import DigestCache, DigestRecord

        cache = DigestCache()
        # Manually add a record.
        cache._records["/tmp/test.txt"] = DigestRecord(
            path="/tmp/test.txt",
            size=100,
            mtime_ns=12345,
            sha256="abc123",
        )

        cache_path = tmp_path / "digest_cache.json"
        cache.save_to(cache_path)

        assert cache_path.is_file()

        loaded = DigestCache.load_from(cache_path)
        assert "/tmp/test.txt" in loaded._records
        rec = loaded._records["/tmp/test.txt"]
        assert rec.size == 100
        assert rec.mtime_ns == 12345
        assert rec.sha256 == "abc123"

    def test_load_missing_file_returns_empty(self, tmp_path):
        """Loading a non-existent cache file returns an empty cache."""
        from acp.evidence.manifest import DigestCache

        cache = DigestCache.load_from(tmp_path / "nonexistent.json")
        assert len(cache._records) == 0

    def test_load_corrupted_file_returns_empty(self, tmp_path):
        """Loading a corrupted cache file returns an empty cache."""
        from acp.evidence.manifest import DigestCache

        cache_path = tmp_path / "digest_cache.json"
        cache_path.write_text("not valid json{{{")
        cache = DigestCache.load_from(cache_path)
        assert len(cache._records) == 0

    def test_verify_persists_cache_in_fast_mode(self, tmp_path):
        """Fast-mode verify writes a digest_cache.json file."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "diff.patch").write_text("diff content")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_hash = _sha256_file(artifacts_dir / "final_report.md")
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        cache_path = run_dir / "digest_cache.json"
        assert not cache_path.exists()

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True
        assert cache_path.exists(), "fast-mode verify should persist digest cache"

    def test_deep_mode_ignores_cache(self, tmp_path):
        """Deep-mode verify does not use or write the digest cache."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "diff.patch").write_text("diff content")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_hash = _sha256_file(artifacts_dir / "final_report.md")
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        cache_path = run_dir / "digest_cache.json"
        result = verify_evidence_manifest(run_dir, deep=True)
        assert result is True
        assert not cache_path.exists(), "deep-mode verify should not write digest cache"


# --------------------------------------------------------------------------- #
# 8. Executor config verification
# --------------------------------------------------------------------------- #


class TestExecutorConfigVerification:
    """Verifier checks executor metadata from sandbox.configured events."""

    def test_open_network_policy_in_event_fails_verify(self, tmp_path):
        """sandbox.configured with network_policy='open' fails verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "diff.patch").write_text("diff content")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        # Write a sandbox.configured event with network_policy='open'.
        events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": "acp-test",
                "sandbox_remote": "sandbox-acp-test",
                "executor": {
                    "backend": "docker_sbx",
                    "network_policy": "open",  # should never be allowed
                    "clone_mode": True,
                    "agent": "claude",
                },
            },
        )
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_hash = _sha256_file(artifacts_dir / "final_report.md")
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is False, "open network_policy in event should fail verify"

    def test_non_clone_mode_in_event_fails_verify(self, tmp_path):
        """A sandbox.configured event with clone_mode=False fails verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "diff.patch").write_text("diff content")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": "acp-test",
                "sandbox_remote": "sandbox-acp-test",
                "executor": {
                    "backend": "docker_sbx",
                    "network_policy": "locked_down",
                    "clone_mode": False,  # should never be allowed
                    "agent": "claude",
                },
            },
        )
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_hash = _sha256_file(artifacts_dir / "final_report.md")
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is False, "clone_mode=False in event should fail verify"

    def test_valid_executor_config_passes_verify(self, tmp_path):
        """A sandbox.configured event with valid config passes verify."""
        from acp.events import EventWriter
        from acp.evidence.manifest import (
            _sha256_file,
            build_evidence_manifest,
            compute_artifact_content_hash,
            verify_evidence_manifest,
        )
        from acp.models import EventType

        run_dir = tmp_path / "run"
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "diff.patch").write_text("diff content")
        (artifacts_dir / "final_report.md").write_text("# Report\n")

        events = EventWriter("task_20260101_0001", run_dir)
        events.write(EventType.TASK_CREATED, {"task_id": "task_20260101_0001"})
        events.write(
            EventType.SANDBOX_CONFIGURED,
            {
                "sandbox_name": "acp-test",
                "sandbox_remote": "sandbox-acp-test",
                "executor": {
                    "backend": "docker_sbx",
                    "network_policy": "locked_down",
                    "clone_mode": True,
                    "agent": "claude",
                },
            },
        )
        real_hash = compute_artifact_content_hash(run_dir)
        events.write(EventType.EVIDENCE_FINALIZED, {"artifact_content_hash": real_hash})
        report_hash = _sha256_file(artifacts_dir / "final_report.md")
        events.write(EventType.EVIDENCE_REPORT_BOUND, {"report_hash": report_hash})

        manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events)
        manifest_path = run_dir / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        result = verify_evidence_manifest(run_dir, deep=False)
        assert result is True
