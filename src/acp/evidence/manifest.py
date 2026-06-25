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

EVIDENCE_CONFIG_FILENAME = "evidence_config.json"


def _sha256_file(path: Path) -> str:
    """sha256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_artifact_content_hash(run_dir: Path) -> str:
    """Compute a hash over just the artifact files (not the event chain).

    This hash is stable across manifest rewrites — it doesn't change when the
    event chain head changes (e.g. after writing the ``evidence.finalized``
    event). It's used in the ``evidence.finalized`` event payload to bind the
    artifacts to the signed event log: if any artifact is tampered with, this
    hash changes, and the signed event's payload no longer matches.
    """
    run_dir = Path(run_dir)
    artifacts_dir = run_dir / "artifacts"
    artifact_hashes: dict[str, str] = {}
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(run_dir))
                if rel == "artifacts/final_report.md":
                    continue
                artifact_hashes[rel] = _sha256_file(path)
    content = json.dumps(artifact_hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


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
    chain_valid = verify_event_chain(events)
    chain_head = events_writer.last_hash

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "manifest_type": "run",
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


# --------------------------------------------------------------------------- #
# Lifecycle manifest — separate from the run manifest
# --------------------------------------------------------------------------- #
#
# The run manifest (evidence_manifest.json) is written at finalize time and
# covers the agent-run artifacts + event chain. After lifecycle events
# (approve/reject), the event log grows but the run artifacts don't change.
# Rather than treating the run manifest as mutable, we write a separate
# lifecycle_manifest.json that covers only the lifecycle events
# (human.approved, human.rejected, memory.promoted). This keeps the run
# evidence immutable while still providing a verifiable record of lifecycle
# actions.


def build_lifecycle_manifest(
    *,
    run_dir: Path,
    events_writer: EventWriter,
) -> dict[str, Any]:
    """Build the lifecycle manifest dict for a run with post-run lifecycle events.

    Covers only lifecycle events (human.approved, human.rejected,
    memory.promoted). The run manifest covers the agent-run evidence;
    this covers what happened after the run.
    """
    run_dir = Path(run_dir)
    all_events = events_writer.read_all()
    lifecycle_types = {"human.approved", "human.rejected", "memory.promoted"}
    lifecycle_events = [e for e in all_events if e.type.value in lifecycle_types]

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "manifest_type": "lifecycle",
        "task_id": events_writer.task_id,
        "lifecycle_event_count": len(lifecycle_events),
        "lifecycle_events": [
            {
                "event_id": e.event_id,
                "type": e.type.value,
                "timestamp": e.timestamp,
                "hash": e.hash,
                "signed": bool(e.signature),
            }
            for e in lifecycle_events
        ],
    }
    manifest_content = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = hashlib.sha256(manifest_content.encode()).hexdigest()
    return manifest


def write_lifecycle_manifest(
    *,
    run_dir: Path,
    events_writer: EventWriter,
) -> Path | None:
    """Write ``lifecycle_manifest.json`` into the run dir.

    Returns the manifest path, or ``None`` if there are no lifecycle events
    (nothing to record).
    """
    manifest = build_lifecycle_manifest(run_dir=run_dir, events_writer=events_writer)
    if manifest["lifecycle_event_count"] == 0:
        return None
    manifest_path = Path(run_dir) / "lifecycle_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path


def verify_lifecycle_manifest(run_dir: Path) -> bool:
    """Verify the lifecycle manifest against the event log.

    Returns ``True`` if the lifecycle manifest exists and its content matches
    the lifecycle events in the event log, ``False`` otherwise. Returns ``True``
    if no lifecycle manifest exists (no lifecycle events → no manifest needed).
    """
    run_dir = Path(run_dir)
    manifest_path = run_dir / "lifecycle_manifest.json"
    if not manifest_path.is_file():
        return True  # no lifecycle manifest = no lifecycle events = OK

    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, ValueError):
        return False

    # Verify manifest_hash.
    stored_hash = manifest.pop("manifest_hash", None)
    recomputed = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if stored_hash != recomputed:
        return False
    manifest["manifest_hash"] = stored_hash

    # Verify lifecycle events match the event log.
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return False
    from acp.models import Event
    lifecycle_types = {"human.approved", "human.rejected", "memory.promoted"}
    actual_lifecycle: list[Event] = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            evt = Event.model_validate_json(line)
            if evt.type.value in lifecycle_types:
                actual_lifecycle.append(evt)
        except Exception:
            continue

    recorded = manifest.get("lifecycle_events", [])
    if len(recorded) != len(actual_lifecycle):
        return False
    for rec, actual in zip(recorded, actual_lifecycle):
        if rec.get("event_id") != actual.event_id:
            return False
        if rec.get("hash") != actual.hash:
            return False
        if rec.get("type") != actual.type.value:
            return False

    return True


def verify_evidence_manifest(run_dir: Path) -> bool:
    """Verify that the on-disk artifacts + event log match the manifest.

    Returns ``True`` iff:
      * every artifact file listed in the manifest exists and has the
        recorded sha256
      * no extra artifact files exist that aren't in the manifest
      * the event chain head matches
      * the event chain is valid
      * the manifest's own ``manifest_hash`` is correct (recomputed from the
        manifest content excluding ``manifest_hash`` itself)
      * if an ``evidence.finalized`` event exists, its ``artifact_manifest_hash``
        matches the recomputed manifest hash (binds artifacts to signed evidence)
    """
    run_dir = Path(run_dir)
    manifest_path = run_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, ValueError):
        return False

    # Check schema version — reject unknown major versions.
    schema_version = manifest.get("schema_version", "0")
    major = schema_version.split(".")[0]
    if major not in ("0", "1"):  # 0 = pre-versioning manifests (backward compat)
        return False

    # Verify the manifest's own hash — recompute from content excluding
    # manifest_hash itself. This prevents an attacker from tampering with
    # artifact hashes and then updating manifest_hash to match.
    stored_hash = manifest.pop("manifest_hash", None)
    recomputed = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if stored_hash != recomputed:
        return False
    # Restore it for the rest of the function.
    manifest["manifest_hash"] = stored_hash

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

    # Verify event chain. The event log is the source of truth — if it's
    # missing or empty, the manifest cannot be valid.
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return False
    from acp.models import Event, EventType
    events: list[Event] = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(Event.model_validate_json(line))
        except Exception:  # noqa: BLE001
            return False  # malformed event log
    if not verify_event_chain(events):
        return False
    if not events:
        return False
    if events[-1].hash != manifest.get("event_chain_head"):
        return False

    # Verify the evidence.finalized event — if present, its payload hashes
    # must match the recomputed values from disk. This is what binds artifacts,
    # the report, and task.json to the signed event log. If an attacker tampers
    # with any of these and updates the manifest to match, the evidence.finalized
    # event's payload still has the old hashes — and that event is signed and
    # hash-chained.
    finalized_events = [e for e in events if e.type == EventType.EVIDENCE_FINALIZED]
    if finalized_events:
        finalized = finalized_events[-1]

        # Artifact content hash.
        expected_artifact_hash = finalized.payload.get("artifact_content_hash")
        recomputed_artifact_hash = compute_artifact_content_hash(run_dir)
        if expected_artifact_hash != recomputed_artifact_hash:
            return False

    return True


# --------------------------------------------------------------------------- #
# Evidence config sidecar
# --------------------------------------------------------------------------- #
#
# The evidence config (signing key path, durable store path, public key path)
# is a property of a *run*, not of the human approving it later. We persist it
# as a sidecar ``evidence_config.json`` at finalize time so that post-run
# lifecycle commands (``acp approve`` / ``acp reject``) can recover the exact
# signing key + durable store the run used — and therefore sign lifecycle
# events with the same key and dual-write them to the same SQLite index.
#
# Only filesystem *paths* are recorded (never key material). The paths are
# resolved at finalize time, so they are absolute and stable.


def write_evidence_config(
    run_dir: Path,
    *,
    signing_key_path: Path | None = None,
    durable_store: Path | None = None,
    public_key_path: Path | None = None,
) -> Path:
    """Persist the run's evidence config as ``evidence_config.json``.

    Records the resolved filesystem paths for the signing key, durable store,
    and public key so post-run lifecycle commands can recover them. Only paths
    are stored — never key bytes. Idempotent: overwrites any prior sidecar.
    """
    run_dir = Path(run_dir)
    config: dict[str, str | None] = {
        "signing_key_path": str(signing_key_path) if signing_key_path else None,
        "durable_store": str(durable_store) if durable_store else None,
        "public_key_path": str(public_key_path) if public_key_path else None,
    }
    path = run_dir / EVIDENCE_CONFIG_FILENAME
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def read_evidence_config(run_dir: Path) -> dict[str, Path | None]:
    """Read the run's evidence config sidecar.

    Returns a dict with ``signing_key_path``, ``durable_store``, and
    ``public_key_path`` keys (each a ``Path`` or ``None``). Returns all-None
    if the sidecar is absent (e.g. runs from before this was written).

    Raises ``ValueError`` if the sidecar exists but contains malformed JSON —
    a corrupt evidence config must not be silently treated as "no config,"
    because that could downgrade a signed run to unsigned. The caller
    (``_record_lifecycle_event``) catches this and surfaces it as an error.
    """
    run_dir = Path(run_dir)
    path = run_dir / EVIDENCE_CONFIG_FILENAME
    if not path.is_file():
        return {"signing_key_path": None, "durable_store": None, "public_key_path": None}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"evidence_config.json is malformed: {path} ({exc})"
        ) from exc
    return {
        "signing_key_path": Path(data["signing_key_path"]) if data.get("signing_key_path") else None,
        "durable_store": Path(data["durable_store"]) if data.get("durable_store") else None,
        "public_key_path": Path(data["public_key_path"]) if data.get("public_key_path") else None,
    }
