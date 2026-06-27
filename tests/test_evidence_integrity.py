"""v0.5.5 tests — event-log hash chain + evidence manifest integrity.

Covers:
  - Event hash chain: each event links to the previous, tamper detection
  - Evidence manifest: content-addressed artifact hashes, event chain head
  - Manifest verification: artifacts + event log match the manifest
  - Report includes the manifest hash
"""

from __future__ import annotations

import json
from pathlib import Path

from acp.events import GENESIS_HASH, EventWriter, verify_event_chain
from acp.evidence.manifest import (
    build_evidence_manifest,
    verify_evidence_manifest,
    write_evidence_manifest,
)
from acp.models import EventType


def _writer(tmp_path: Path) -> EventWriter:
    return EventWriter("test_task", tmp_path)


def test_event_chain_genesis_first_event(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    events = w.read_all()
    assert len(events) == 1
    assert events[0].prev_hash == GENESIS_HASH
    assert events[0].hash != ""
    assert verify_event_chain(events) is True


def test_event_chain_links_correctly(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})
    events = w.read_all()
    assert len(events) == 3
    # Each event's prev_hash is the previous event's hash.
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash
    assert verify_event_chain(events) is True


def test_event_chain_detects_tampered_payload(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    events = w.read_all()
    assert verify_event_chain(events) is True
    # Tamper with the first event's payload.
    events[0].payload["request"] = "tampered"
    assert verify_event_chain(events) is False


def test_event_chain_detects_removed_event(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})
    events = w.read_all()
    assert verify_event_chain(events) is True
    # Remove the middle event — chain breaks.
    assert verify_event_chain([events[0], events[2]]) is False


def test_event_chain_detects_reordered_events(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    events = w.read_all()
    assert verify_event_chain(events) is True
    # Reorder — chain breaks.
    assert verify_event_chain([events[1], events[0]]) is False


def test_event_chain_empty_list_is_invalid(tmp_path: Path):
    """An empty event log has no evidence trail — verification must fail."""
    assert verify_event_chain([]) is False


def test_event_writer_resumes_chain_after_restart(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.REPO_CHECKED, {"clean": True})
    first_chain_head = w.last_hash

    # Simulate a restart: create a new writer pointing at the same log.
    w2 = EventWriter("test_task", tmp_path)
    assert w2.count == 2
    assert w2.last_hash == first_chain_head

    # The next event should link to the last event from the first writer.
    w2.write(EventType.TASK_COMPLETED, {"status": "passed"})
    events = w2.read_all()
    assert len(events) == 3
    assert events[2].prev_hash == first_chain_head
    assert verify_event_chain(events) is True


# --------------------------------------------------------------------------- #
# Evidence manifest
# --------------------------------------------------------------------------- #


def test_manifest_hashes_artifacts(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "diff.patch").write_text("some diff\n")
    (artifacts / "commands.json").write_text("[]\n")

    manifest = build_evidence_manifest(run_dir=tmp_path, events_writer=w)
    assert "artifacts/diff.patch" in manifest["artifacts"]
    assert "artifacts/commands.json" in manifest["artifacts"]
    assert manifest["event_chain_valid"] is True
    assert manifest["event_chain_head"] == w.last_hash
    assert manifest["manifest_hash"] != ""


def test_manifest_excludes_final_report(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "final_report.md").write_text("# report\n")
    (artifacts / "diff.patch").write_text("diff\n")

    manifest = build_evidence_manifest(run_dir=tmp_path, events_writer=w)
    assert "artifacts/final_report.md" not in manifest["artifacts"]
    assert "artifacts/diff.patch" in manifest["artifacts"]


def test_write_and_verify_manifest_roundtrip(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    w.write(EventType.TASK_COMPLETED, {"status": "passed"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "diff.patch").write_text("diff\n")
    (artifacts / "commands.json").write_text("[]\n")

    manifest_path, manifest_hash = write_evidence_manifest(
        run_dir=tmp_path,
        events_writer=w,
    )
    assert manifest_path.is_file()
    assert manifest_hash != ""

    manifest = json.loads(manifest_path.read_text())
    assert manifest["manifest_hash"] == manifest_hash

    # Verification passes.
    assert verify_evidence_manifest(tmp_path) is True


def test_manifest_verification_fails_on_tampered_artifact(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "diff.patch").write_text("original\n")

    write_evidence_manifest(run_dir=tmp_path, events_writer=w)

    # Tamper with the artifact.
    (artifacts / "diff.patch").write_text("tampered\n")
    assert verify_evidence_manifest(tmp_path) is False


def test_manifest_verification_fails_on_extra_artifact(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "diff.patch").write_text("diff\n")

    write_evidence_manifest(run_dir=tmp_path, events_writer=w)

    # Add an extra file not in the manifest.
    (artifacts / "extra.txt").write_text("extra\n")
    assert verify_evidence_manifest(tmp_path) is False


def test_manifest_verification_fails_on_tampered_event_chain(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(EventType.TASK_CREATED, {"request": "test"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "diff.patch").write_text("diff\n")

    write_evidence_manifest(run_dir=tmp_path, events_writer=w)

    # Tamper with the event log — rewrite it with a modified payload.
    events_path = tmp_path / "events.jsonl"
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    events[0]["payload"]["request"] = "tampered"
    # Recompute the hash to be wrong (simulate a naive tamper).
    events[0]["hash"] = "wrong"
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    assert verify_evidence_manifest(tmp_path) is False
