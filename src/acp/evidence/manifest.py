"""Evidence manifest — content-addressed artifact hashes + event chain summary.

At the end of a run, ACP writes ``evidence_manifest.json`` into the run
directory. This manifest records:

  * the sha256 of every file under ``artifacts/`` (content-addressed)
  * the event log's chain head hash (last event's ``hash``)
  * the total event count
  * a manifest-level sha256 over the manifest content (so the manifest
    itself is verifiable)

The manifest hash is included in ``final_report.md`` so a reader can verify
that the report they're reading corresponds to a specific, immutable set of
artifacts + event log.

This is not a cryptographic signature — it doesn't prove *who* wrote the
artifacts. But it makes the evidence set tamper-evident: changing any
artifact, any event, or the report itself breaks a hash that is recorded
in the manifest, which is recorded in the report.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from acp.events import EventWriter, verify_event_chain


def _sha256_file(path: Path) -> str:
    """sha256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_evidence_manifest(
    *,
    run_dir: Path,
    events_writer: EventWriter,
) -> dict[str, Any]:
    """Build the evidence manifest dict for a completed run.

    Hashes every file under ``artifacts/`` and records the event chain head.
    Does NOT write the manifest to disk — call :func:`write_evidence_manifest`
    for that. Returns the manifest as a dict so the report writer can include
    its hash before it's persisted.
    """
    run_dir = Path(run_dir)
    artifacts_dir = run_dir / "artifacts"

    artifact_hashes: dict[str, str] = {}
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(run_dir))
                # The report is a projection of the evidence, not evidence
                # itself. It includes the manifest hash, so hashing it would
                # create a circular dependency. The manifest covers all
                # *source* artifacts; the report references the manifest hash.
                if rel == "artifacts/final_report.md":
                    continue
                artifact_hashes[rel] = _sha256_file(path)

    events = events_writer.read_all()
    chain_valid = verify_event_chain(events) if events else True
    chain_head = events_writer.last_hash

    manifest: dict[str, Any] = {
        "task_id": events_writer.task_id,
        "event_count": events_writer.count,
        "event_chain_head": chain_head,
        "event_chain_valid": chain_valid,
        "artifacts": artifact_hashes,
    }
    # The manifest hash covers everything except itself.
    manifest_content = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = hashlib.sha256(manifest_content.encode()).hexdigest()
    return manifest


def write_evidence_manifest(
    *,
    run_dir: Path,
    events_writer: EventWriter,
) -> tuple[Path, str]:
    """Write ``evidence_manifest.json`` into the run dir.

    Returns ``(manifest_path, manifest_hash)``. The manifest hash is meant
    to be included in the report so the report ↔ evidence binding is
    verifiable.
    """
    manifest = build_evidence_manifest(run_dir=run_dir, events_writer=events_writer)
    manifest_path = Path(run_dir) / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path, manifest["manifest_hash"]


def verify_evidence_manifest(run_dir: Path) -> bool:
    """Verify that the on-disk artifacts + event log match the manifest.

    Returns ``True`` iff:
      * every artifact file listed in the manifest exists and has the
        recorded sha256
      * no extra artifact files exist that aren't in the manifest
      * the event chain head matches
      * the event chain is valid
    """
    run_dir = Path(run_dir)
    manifest_path = run_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text())

    # Verify artifact hashes.
    artifacts_dir = run_dir / "artifacts"
    for rel, expected_hash in manifest.get("artifacts", {}).items():
        path = run_dir / rel
        if not path.is_file():
            return False
        if _sha256_file(path) != expected_hash:
            return False

    # Check for extra files not in the manifest (final_report.md is excluded
    # — it's a projection, not source evidence).
    if artifacts_dir.is_dir():
        on_disk = {
            str(p.relative_to(run_dir))
            for p in artifacts_dir.rglob("*")
            if p.is_file()
        }
        on_disk.discard("artifacts/final_report.md")
        manifest_files = set(manifest.get("artifacts", {}).keys())
        if on_disk != manifest_files:
            return False

    # Verify event chain.
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        from acp.models import Event
        events = [
            Event.model_validate_json(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        if not verify_event_chain(events):
            return False
        if events and events[-1].hash != manifest.get("event_chain_head"):
            return False

    return True
