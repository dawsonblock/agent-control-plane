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

v0.5.11 extends the binding: the ``evidence.finalized`` event now also
carries ``task_json_hash`` (binding the full task metadata), and a separate
``evidence.report_bound`` event carries ``report_hash`` (binding the
human-facing report). Together with ``artifact_content_hash``, these three
hashes bind the complete evidence record — artifacts, task metadata, and
report — to the signed, hash-chained event log. Tampering with any of them
breaks a hash recorded in a signed event.

This is not a cryptographic signature — it doesn't prove *who* wrote the
artifacts. But it makes the evidence set tamper-evident: changing any
artifact, the report, task.json, or any event breaks a hash that is
recorded in a signed event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.events import EventWriter, verify_event_chain

EVIDENCE_CONFIG_FILENAME = "evidence_config.json"

# Default ignore rules — generated/heavy paths that should never be hashed
# as evidence. These are not ACP-created artifacts; they're build/dependency
# junk that would waste compute and inflate manifests.
DEFAULT_IGNORE_PATTERNS: frozenset[str] = frozenset({
    "__pycache__", ".pytest_cache", ".venv", "node_modules",
    ".git", "dist", "build", "coverage", ".mypy_cache", ".ruff_cache",
    "*.pyc", "*.pyo", "*.egg-info",
})


@dataclass(frozen=True)
class DigestRecord:
    """Cached digest entry — keyed by path, size, and mtime_ns.

    If a file's size and mtime_ns haven't changed, the cached sha256 is
    reused. This avoids re-hashing unchanged files on every verify.
    """
    path: str
    size: int
    mtime_ns: int
    sha256: str


class DigestCache:
    """Path → DigestRecord cache for streaming file digests.

    Reuses cached digests when (size, mtime_ns) match. Re-hashes from disk
    only when the file has changed. Files are streamed in 1 MB chunks —
    never read entirely into memory.

    v0.5.15: The cache can be persisted to disk as ``digest_cache.json``
    so that repeated ``acp verify`` calls don't re-hash unchanged files.
    The cache is never the trust root — ``--deep`` mode ignores it and
    recomputes all hashes from scratch.
    """

    def __init__(self) -> None:
        self._records: dict[str, DigestRecord] = {}

    def digest(self, path: Path) -> str:
        """Return the sha256 of ``path``, using the cache if possible."""
        path = Path(path)
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            # Can't stat — fall through to direct hash (will likely fail too).
            return _sha256_file(path)
        cached = self._records.get(key)
        if cached is not None and cached.size == stat.st_size and cached.mtime_ns == stat.st_mtime_ns:
            return cached.sha256
        result = _sha256_file(path)
        self._records[key] = DigestRecord(
            path=key, size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=result,
        )
        return result

    # v0.5.15: Persistence — save/load the cache to/from disk.

    def save_to(self, path: Path) -> None:
        """Persist the cache to ``path`` as JSON.

        The cache file is written atomically (temp file + rename) so a
        crash mid-write doesn't corrupt it.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            str(r.path): {
                "size": r.size,
                "mtime_ns": r.mtime_ns,
                "sha256": r.sha256,
            }
            for r in self._records.values()
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))
        tmp.rename(path)

    @classmethod
    def load_from(cls, path: Path) -> DigestCache:
        """Load a cache from ``path``. Returns an empty cache if file missing."""
        cache = cls()
        path = Path(path)
        if not path.is_file():
            return cache
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cache  # corrupted cache — start fresh
        for key, rec in data.items():
            try:
                cache._records[key] = DigestRecord(
                    path=key,
                    size=rec["size"],
                    mtime_ns=rec["mtime_ns"],
                    sha256=rec["sha256"],
                )
            except (KeyError, TypeError):
                continue  # skip malformed entries
        return cache


def _sha256_file(path: Path) -> str:
    """sha256 hex digest of a file's contents (streamed in 1 MB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _should_ignore_path(path: Path, relative_to: Path) -> bool:
    """Check if a path matches any DEFAULT_IGNORE_PATTERNS entry.

    Supports both exact directory/file name matches (e.g. ``__pycache__``)
    and glob patterns (e.g. ``*.pyc``).
    """
    import fnmatch
    rel = str(path.relative_to(relative_to))
    parts = path.parts
    for pattern in DEFAULT_IGNORE_PATTERNS:
        # Check if any path component matches the pattern.
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def compute_artifact_content_hash(run_dir: Path) -> str:
    """Compute a hash over just the artifact files (not the event chain).

    This hash is stable across manifest rewrites — it doesn't change when the
    event chain head changes (e.g. after writing the ``evidence.finalized``
    event). It's used in the ``evidence.finalized`` event payload to bind the
    artifacts to the signed event log: if any artifact is tampered with, this
    hash changes, and the signed event's payload no longer matches.

    Files matching DEFAULT_IGNORE_PATTERNS (``__pycache__``, ``*.pyc``, etc.)
    are excluded — they are generated junk, not evidence.
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
                if _should_ignore_path(path, run_dir):
                    continue
                artifact_hashes[rel] = _sha256_file(path)
    content = json.dumps(artifact_hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def compute_task_json_hash(run_dir: Path) -> str | None:
    """Compute a sha256 over the immutable fields of the run's ``task.json``.

    Returns ``None`` if ``task.json`` does not exist. The hash covers only
    the *immutable* identity fields of the task (task_id, repo_name,
    repo_path, base_branch, base_commit_sha, task_branch, worktree_path,
    user_request, created_at) — not the mutable fields (status, updated_at)
    which change during lifecycle transitions (approve/reject). This means
    a status change during a signed lifecycle event does NOT break the
    binding, but tampering with ``user_request``, ``repo_name``,
    ``worktree_path``, or any other identity field does.

    The hash uses canonical JSON (parsed + re-serialized with ``sort_keys``)
    so it's deterministic regardless of key ordering in the source file.
    """
    run_dir = Path(run_dir)
    task_json_path = run_dir / "task.json"
    if not task_json_path.is_file():
        return None
    try:
        data = json.loads(task_json_path.read_text())
    except (json.JSONDecodeError, ValueError):
        return None
    # Keep only immutable identity fields — exclude status + updated_at
    # which are lifecycle-mutable.
    immutable_fields = (
        "task_id", "repo_name", "repo_path", "base_branch",
        "base_commit_sha", "task_branch", "worktree_path",
        "user_request", "created_at",
    )
    immutable = {k: data[k] for k in immutable_fields if k in data}
    return hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compute_report_hash(run_dir: Path) -> str | None:
    """Compute a sha256 over the run's ``final_report.md`` file content.

    Returns ``None`` if the report does not exist. This hash is recorded in
    the ``evidence.report_bound`` event to bind the human-facing report to
    the signed event log. Tampering with or deleting the report breaks this
    binding.
    """
    run_dir = Path(run_dir)
    report_path = run_dir / "artifacts" / "final_report.md"
    if not report_path.is_file():
        return None
    return _sha256_file(report_path)


def derive_status_from_events(events: list[Any]) -> str | None:
    """Derive the expected task status from the event log.

    The event log is the source of truth; task.json is only a projection.
    This function scans the event log for terminal + lifecycle events and
    returns the status that task.json *should* have:

      * human.approved → "approved"
      * human.rejected → "rejected" (terminal — rejection overrides run status)
      * task.completed → "passed"
      * task.failed → "failed"
      * task.needs_review → "needs_review"
      * none of the above → None (can't determine)

    Lifecycle events take precedence over run-terminal events: if both
    task.completed and human.approved exist, the status is "approved".
    human.rejected is terminal — once rejected, the status stays "rejected"
    even if a later human.approved exists (which shouldn't happen, but we
    guard against it).
    """
    from acp.models import EventType

    has_approved = False
    has_rejected = False
    run_status: str | None = None

    for evt in events:
        if evt.type == EventType.HUMAN_APPROVED:
            has_approved = True
        elif evt.type == EventType.AUTO_APPROVED:
            has_approved = True  # auto.approved is equivalent
        elif evt.type == EventType.HUMAN_REJECTED:
            has_rejected = True
        elif evt.type == EventType.TASK_COMPLETED:
            run_status = "passed"
        elif evt.type == EventType.TASK_FAILED:
            run_status = "failed"
        elif evt.type == EventType.TASK_NEEDS_REVIEW:
            run_status = "needs_review"

    # Lifecycle events take precedence over run-terminal events.
    # v0.5.16: Rejection maps to TaskStatus.REJECTED (a first-class human
    # decision). Rejection is terminal and overrides approval.
    if has_rejected:
        return "rejected"
    if has_approved:
        return "approved"
    return run_status


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
                # v0.5.15: Apply the same ignore rules as
                # compute_artifact_content_hash. Ignored/generated files
                # (e.g. __pycache__, *.pyc) must not appear in the manifest
                # at all, so fast and deep verification agree on what counts
                # as evidence.
                if _should_ignore_path(path, run_dir):
                    continue
                artifact_hashes[rel] = _sha256_file(path)

    events = events_writer.read_all()
    chain_valid = verify_event_chain(events)

    # v0.5.15: The event_chain_head covers only run-phase events — up to
    # evidence.finalized. Post-run events (evidence.report_bound, lifecycle,
    # sandbox cleanup) are NOT included in the run manifest's chain head.
    # This keeps the run manifest immutable while post-run events are
    # verified separately.
    lifecycle_types = {
        "human.approved", "human.rejected", "memory.promoted",
        # v0.6.0: Autonomous mode lifecycle events.
        "auto.approved", "auto.merged",
    }
    sandbox_types = {
        "sandbox.configured", "sandbox.started",
        "sandbox.failed", "sandbox.stopped",
    }
    post_run_types = lifecycle_types | {"evidence.report_bound"} | sandbox_types
    run_phase_events = [e for e in events if e.type.value not in post_run_types]
    chain_head = run_phase_events[-1].hash if run_phase_events else events_writer.last_hash

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
    report_hash: str | None = None,
) -> dict[str, Any]:
    """Build the lifecycle manifest dict for a run with post-run lifecycle events.

    Covers only lifecycle events (human.approved, human.rejected,
    memory.promoted). The run manifest covers the agent-run evidence;
    this covers what happened after the run. When ``report_hash`` is provided
    (the hash of the re-rendered report after lifecycle), it's included so
    ``acp verify`` can bind the post-lifecycle report to the lifecycle record.
    """
    run_dir = Path(run_dir)
    all_events = events_writer.read_all()
    lifecycle_types = {
        "human.approved", "human.rejected", "memory.promoted",
        "auto.approved", "auto.merged",
    }
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
    if report_hash is not None:
        manifest["report_hash"] = report_hash
    manifest_content = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = hashlib.sha256(manifest_content.encode()).hexdigest()
    return manifest


def write_lifecycle_manifest(
    *,
    run_dir: Path,
    events_writer: EventWriter,
    report_hash: str | None = None,
) -> Path | None:
    """Write ``lifecycle_manifest.json`` into the run dir.

    Returns the manifest path, or ``None`` if there are no lifecycle events
    (nothing to record). When ``report_hash`` is provided, it's included in
    the manifest so post-lifecycle report verification works.
    """
    manifest = build_lifecycle_manifest(
        run_dir=run_dir, events_writer=events_writer, report_hash=report_hash
    )
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
    lifecycle_types = {
        "human.approved", "human.rejected", "memory.promoted",
        "auto.approved", "auto.merged",
    }
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


def verify_evidence_manifest(run_dir: Path, *, deep: bool = False) -> bool:
    """Verify that the on-disk artifacts + event log match the manifest.

    Returns ``True`` iff:
      * the manifest's own ``manifest_hash`` is correct (recomputed from the
        manifest content excluding ``manifest_hash`` itself)
      * the event chain head matches the last run-phase event
      * the event chain is valid
      * if an ``evidence.finalized`` event exists, its ``artifact_content_hash``
        and ``task_json_hash`` match the recomputed values from disk
      * if an ``evidence.report_bound`` event exists, its ``report_hash``
        matches the on-disk report (or the lifecycle manifest's report_hash
        if lifecycle events have re-rendered the report)

    In **fast mode** (default, ``deep=False``): the ``artifact_content_hash``
    in ``evidence.finalized`` covers all artifacts (it's a hash of all
    artifact hashes), so individual artifact hash recompute is skipped.
    This is sufficient for tamper detection — any artifact change breaks
    the content hash.

    In **deep mode** (``deep=True``): every individual artifact hash is
    recomputed from disk and compared against the manifest, AND extra-file
    detection runs (no files exist that aren't in the manifest). Use this
    in CI/nightly for stricter verification.

    Task.json status changes are allowed when a corresponding signed lifecycle
    event (human.approved / human.rejected) exists — the status field is the
    only field permitted to change post-finalize.
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

    # In deep mode, verify individual artifact hashes and check for extra
    # files. In fast mode, the artifact_content_hash in evidence.finalized
    # covers all artifacts — individual recompute is redundant. But for
    # older runs without evidence.finalized, fast mode falls back to
    # individual hash checks (there's no content hash to rely on).
    # v0.5.15: Persist the DigestCache to disk so repeated verify calls
    # don't re-hash unchanged files. In deep mode, the cache is NOT used
    # — deep mode recomputes everything from scratch (the cache is never
    # the trust root). In fast mode, the cache is loaded from disk, used
    # for any individual hash checks, and saved back at the end.
    cache_path = run_dir / "digest_cache.json"
    if deep:
        cache = None
    else:
        cache = DigestCache.load_from(cache_path)
    artifacts_dir = run_dir / "artifacts"

    # Peek at the event log to determine if evidence.finalized exists.
    # (We'll parse it fully below, but we need this flag now.)
    from acp.models import Event as _PeekEvent
    events_path_peek = run_dir / "events.jsonl"
    has_finalized_peek = False
    if events_path_peek.is_file():
        for line in events_path_peek.read_text().splitlines():
            if not line.strip():
                continue
            try:
                peek_evt = _PeekEvent.model_validate_json(line)
                if peek_evt.type.value == "evidence.finalized":
                    has_finalized_peek = True
                    break
            except Exception:
                pass

    # Fast mode can skip individual hash recompute only when evidence.finalized
    # exists (its artifact_content_hash covers all artifacts). Otherwise,
    # fall back to full individual hash checks.
    can_skip_individual = (not deep) and has_finalized_peek

    if deep or not can_skip_individual:
        for rel, expected_hash in manifest.get("artifacts", {}).items():
            path = run_dir / rel
            if not path.is_file():
                return False
            actual = cache.digest(path) if cache else _sha256_file(path)
            if actual != expected_hash:
                return False

        # Check for extra files not in the manifest (final_report.md is excluded
        # — it's a projection, not source evidence).
        # v0.5.15: Apply the same ignore rules as build_evidence_manifest
        # and compute_artifact_content_hash. Ignored/generated files (e.g.
        # __pycache__, *.pyc) must not trigger a deep verification failure.
        if artifacts_dir.is_dir():
            on_disk = {
                str(p.relative_to(run_dir))
                for p in artifacts_dir.rglob("*")
                if p.is_file() and not _should_ignore_path(p, run_dir)
            }
            on_disk.discard("artifacts/final_report.md")
            manifest_files = set(manifest.get("artifacts", {}).keys())
            if on_disk != manifest_files:
                return False
    else:
        # Fast mode with evidence.finalized: just check that artifact files
        # exist (don't recompute hashes — artifact_content_hash covers that).
        # Still check for missing files, as a deleted artifact changes the
        # content hash.
        for rel in manifest.get("artifacts", {}).keys():
            path = run_dir / rel
            if not path.is_file():
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

    # Detect lifecycle + post-run events.
    lifecycle_types = {
        "human.approved", "human.rejected", "memory.promoted",
        "auto.approved", "auto.merged",
    }
    # v0.5.15: Sandbox events are executor lifecycle events, not run-phase
    # events. They happen after evidence.finalized (cleanup) or before the
    # agent runs (configured). The run manifest's event_chain_head covers
    # only run-phase events up to evidence.finalized. Sandbox cleanup events
    # (sandbox.stopped, sandbox.failed) must not break the run manifest.
    sandbox_types = {
        "sandbox.configured", "sandbox.started",
        "sandbox.failed", "sandbox.stopped",
    }
    post_run_types = lifecycle_types | {"evidence.report_bound"} | sandbox_types
    has_lifecycle = any(e.type.value in lifecycle_types for e in events)

    # The manifest's event_chain_head covers the run phase only — up to
    # evidence.finalized. Post-run events (evidence.report_bound, lifecycle,
    # sandbox cleanup) are NOT included in the run manifest's chain head.
    # This keeps the run manifest immutable while post-run events are
    # verified separately.
    run_phase_events = [e for e in events if e.type.value not in post_run_types]
    if run_phase_events:
        if run_phase_events[-1].hash != manifest.get("event_chain_head"):
            return False
    elif events[-1].hash != manifest.get("event_chain_head"):
        # No run-phase events found (shouldn't happen for a valid run, but
        # fall back to the last event for backward compat).
        return False

    # Verify the evidence.finalized event — if present, its payload hashes
    # must match the recomputed values from disk. This is what binds artifacts,
    # task.json, and (indirectly via report_bound) the report to the signed
    # event log. If an attacker tampers with any of these and updates the
    # manifest to match, the evidence.finalized event's payload still has the
    # old hashes — and that event is signed and hash-chained.
    finalized_events = [e for e in events if e.type == EventType.EVIDENCE_FINALIZED]
    if finalized_events:
        finalized = finalized_events[-1]

        # Artifact content hash.
        expected_artifact_hash = finalized.payload.get("artifact_content_hash")
        recomputed_artifact_hash = compute_artifact_content_hash(run_dir)
        if expected_artifact_hash != recomputed_artifact_hash:
            return False

        # Task.json hash — covers immutable identity fields only. Status and
        # updated_at are excluded (they change during lifecycle transitions),
        # so a signed approve/reject doesn't break this binding. Tampering
        # with user_request, repo_name, worktree_path, etc. does.
        expected_task_hash = finalized.payload.get("task_json_hash")
        if expected_task_hash is not None:
            recomputed_task_hash = compute_task_json_hash(run_dir)
            if recomputed_task_hash != expected_task_hash:
                return False

        # Evidence config hash — binds the evidence *policy* (durable_mode,
        # durable_store, signing_key_path, public_key_path) to the signed
        # event log. An operator who downgrades durable_mode from required
        # to best_effort (or changes the durable_store path) after finalize
        # breaks this binding.
        expected_config_hash = finalized.payload.get("evidence_config_hash")
        if expected_config_hash is not None:
            recomputed_config_hash = compute_evidence_config_hash(run_dir)
            if recomputed_config_hash != expected_config_hash:
                return False

    # v0.5.16: Verify executor config from sandbox.configured event.
    # If a sandbox.configured event exists, its executor metadata is part
    # of the signed event log. We verify that the recorded network_policy
    # and clone_mode match what was declared — an attacker who downgrades
    # the network policy after the run breaks this signed binding.
    sandbox_configured_events = [
        e for e in events if e.type == EventType.SANDBOX_CONFIGURED
    ]
    if sandbox_configured_events:
        configured = sandbox_configured_events[-1]
        executor_meta = configured.payload.get("executor", {})
        # Verify that the recorded network_policy is not "open" — this
        # should have been caught at validation time, but the verifier
        # double-checks the signed event log.
        recorded_policy = executor_meta.get("network_policy", "")
        if recorded_policy == "open":
            return False  # open network policy is never allowed
        # Verify clone_mode is True — ACP requires clone mode.
        recorded_clone = executor_meta.get("clone_mode", False)
        if not recorded_clone:
            return False  # non-clone mode is never allowed

    # Verify the evidence.report_bound event — if present, its report_hash
    # must match the on-disk report. After lifecycle events, a SECOND
    # evidence.report_bound event is written with the re-rendered report's
    # hash. We check the LAST report_bound event — it's the most recent
    # binding and is signed + hash-chained. An attacker who tampers with
    # the report breaks this signed binding, regardless of lifecycle state.
    report_bound_events = [e for e in events if e.type == EventType.EVIDENCE_REPORT_BOUND]
    if report_bound_events:
        report_bound = report_bound_events[-1]
        expected_report_hash = report_bound.payload.get("report_hash")
        if expected_report_hash is not None:
            report_path = run_dir / "artifacts" / "final_report.md"
            if not report_path.is_file():
                return False  # missing report is always fatal
            actual_report_hash = _sha256_file(report_path)
            if actual_report_hash != expected_report_hash:
                return False  # report tampered with or doesn't match signed binding

    # v0.5.15: Persist the digest cache for future fast-mode verifications.
    # Only save if we actually used the cache (fast mode) AND verification
    # passed. Deep mode never populates the cache. We never persist a cache
    # from a failed verification — that could cache incorrect digests.
    if cache is not None:
        try:
            cache.save_to(cache_path)
        except OSError:
            pass  # cache persistence is best-effort, not critical

    return True


# --------------------------------------------------------------------------- #
# Evidence config sidecar
# --------------------------------------------------------------------------- #
#
# The evidence config (signing key path, durable store path, public key path,
# durable mode) is a property of a *run*, not of the human approving it later.
# We persist it as a sidecar ``evidence_config.json`` at finalize time so that
# post-run lifecycle commands (``acp approve`` / ``acp reject``) can recover
# the exact signing key + durable store + durable mode the run used — and
# therefore sign lifecycle events with the same key, dual-write them to the
# same SQLite index, and fail closed if the durable store is required but
# unavailable.
#
# Only filesystem *paths* are recorded (never key material). The paths are
# resolved at finalize time, so they are absolute and stable.


def write_evidence_config(
    run_dir: Path,
    *,
    signing_key_path: Path | None = None,
    durable_store: Path | None = None,
    public_key_path: Path | None = None,
    durable_mode: Any = None,
) -> Path:
    """Persist the run's evidence config as ``evidence_config.json``.

    Records the resolved filesystem paths for the signing key, durable store,
    and public key, plus the durable mode (disabled/best_effort/required) so
    post-run lifecycle commands can recover them. Only paths are stored —
    never key bytes. Idempotent: overwrites any prior sidecar.
    """
    run_dir = Path(run_dir)
    # durable_mode may be a DurableMode enum or a plain string.
    durable_mode_str: str | None = None
    if durable_mode is not None:
        durable_mode_str = getattr(durable_mode, "value", str(durable_mode))
    config: dict[str, str | None] = {
        "signing_key_path": str(signing_key_path) if signing_key_path else None,
        "durable_store": str(durable_store) if durable_store else None,
        "public_key_path": str(public_key_path) if public_key_path else None,
        "durable_mode": durable_mode_str,
    }
    path = run_dir / EVIDENCE_CONFIG_FILENAME
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def read_evidence_config(run_dir: Path) -> dict[str, Path | None | str]:
    """Read the run's evidence config sidecar.

    Returns a dict with ``signing_key_path``, ``durable_store``,
    ``public_key_path`` (each a ``Path`` or ``None``), and ``durable_mode``
    (a ``str`` or ``None``). Returns all-None if the sidecar is absent
    (e.g. runs from before this was written).

    Raises ``ValueError`` if the sidecar exists but contains malformed JSON —
    a corrupt evidence config must not be silently treated as "no config,"
    because that could downgrade a signed run to unsigned. The caller
    (``_record_lifecycle_event``) catches this and surfaces it as an error.
    """
    run_dir = Path(run_dir)
    path = run_dir / EVIDENCE_CONFIG_FILENAME
    if not path.is_file():
        return {
            "signing_key_path": None,
            "durable_store": None,
            "public_key_path": None,
            "durable_mode": None,
        }
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
        "durable_mode": data.get("durable_mode"),
    }


def compute_evidence_config_hash(run_dir: Path) -> str | None:
    """Compute a sha256 over the run's ``evidence_config.json`` content.

    This hash is recorded in the ``evidence.finalized`` event to bind the
    evidence *policy* (durable_mode, durable_store path, signing_key_path,
    public_key_path) to the signed event log. Tampering with any of these
    fields after finalize breaks the binding — an operator cannot silently
    downgrade ``durable_mode`` from ``required`` to ``best_effort`` without
    detection.

    Only the resolved path *strings* are hashed (never key material). The
    hash uses canonical JSON for determinism.
    """
    run_dir = Path(run_dir)
    config_path = run_dir / EVIDENCE_CONFIG_FILENAME
    if not config_path.is_file():
        return None
    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, ValueError):
        return None
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
