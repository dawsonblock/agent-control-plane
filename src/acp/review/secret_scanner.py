"""Secret scanner — detects secret-like strings in a captured diff.

M5's hard-block trigger: if the scanner finds anything that looks like a
real credential, the reviewer marks ``hard_block=True`` → REJECT, and the
report surfaces exactly what was found so a human can confirm and rotate.

The scanner is deliberately conservative about false positives: known
placeholder tokens (`YOUR_...`, `<...>`, `...`, `example`, `changeme`) are
excluded, and high-entropy detection only fires on assignment-shaped lines
(``KEY = "..."``) where the value is long enough to plausibly be a secret
rather than a normal string. It will miss things; treat it as a tripwire,
not a guarantee (see docs/safety.md).

Scans only *added* diff lines (lines starting with ``+``), so deletions and
context never produce findings.

v0.5.14: TruffleHog integration. When TruffleHog is installed, the scanner
uses it for verified detection — TruffleHog checks if a key is live before
flagging it, eliminating false positives. Falls back to the regex scanner
when TruffleHog is not available.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# --- known credential prefixes / shapes ------------------------------------ #
# Each entry: (label, compiled regex). Matched against added lines.
_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # AWS secret: a 40-char base64-ish value on a line whose key mentions
    # secret/priv (catches AWS_SECRET_ACCESS_KEY, aws_secret, privateKey...).
    (
        "aws_secret",
        re.compile(
            r"(?i)^(?=[^\n]*(?:secret|priv)).{0,40}[=:]\s*['\"]?[A-Za-z0-9/+=]{40}(?:['\"]|\s|$)"
        ),
    ),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_legacy_token", re.compile(r"\b[a-f0-9]{40}\b")),  # 40-hex legacy; gated below
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # OpenAI: allow a hyphenated project prefix like sk-proj-...
    ("openai_key", re.compile(r"\bsk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{24,}\b")),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

# Assignment-shaped lines: KEY = "..." / KEY: '...' / export KEY=...
# We extract the value and run entropy + placeholder checks on it.
_ASSIGNMENT_RE = re.compile(
    r"(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
)

# Placeholders / examples that must NEVER count as secrets.
_PLACEHOLDER_RE = re.compile(
    r"(?i)"
    r"(your_|my_|the_|replace_|example|sample|dummy|fake|placeholder|todo|xxxx|"
    r"changeme|change_me|abcdef|12345678|test_test|<[^>]+>|\.\.\.\.*)"  # <token>, ....
)

# Thresholds for entropy-based detection on assignment values.
_MIN_ENTROPY = 3.5  # bits/char; real secrets cluster 3.5–6
_MIN_VALUE_LEN = 20  # ignore short assignment values
_MIN_VOWEL_FRACTION = 0.0  # informational; kept for tuning


@dataclass
class SecretFinding:
    """One detected secret. The reviewer turns this into a hard-block signal."""

    kind: str  # provider label or "high_entropy_assignment"
    snippet: str  # redacted excerpt for the report (never the full secret)
    line_no: int  # line in the patch where it appeared
    entropy: float = 0.0  # bits/char for high_entropy_assignment; 0.0 otherwise


def scan_patch(
    patch: str,
    *,
    custom_regexes: list[tuple[str, re.Pattern[str]]] | None = None,
) -> list[SecretFinding]:
    """Scan added lines of a unified diff patch for secret-like content.

    When ``custom_regexes`` is provided, each (name, pattern) pair is
    checked against added lines in addition to the built-in provider
    patterns. Custom matches are tagged with kind ``custom:<name>`` and
    are included in hard-block detection.
    """
    findings: list[SecretFinding] = []
    seen: set[tuple[str, int]] = set()  # dedupe (kind, line_no)

    # Merge built-in and custom patterns.
    all_patterns = list(_PROVIDER_PATTERNS)
    if custom_regexes:
        all_patterns.extend(custom_regexes)

    for idx, raw_line in enumerate(patch.splitlines(), start=1):
        # Only added lines carry new secret risk.
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        added = raw_line[1:]

        for kind, pat in all_patterns:
            for m in pat.finditer(added):
                key = (kind, idx)
                if key in seen:
                    continue
                # Guard the 40-hex legacy-token rule against commit SHAs in
                # comments: require the match to be inside a string or assignment.
                if kind == "github_legacy_token" and not _looks_assigned(added, m.group(0)):
                    continue
                seen.add(key)
                findings.append(SecretFinding(kind=kind, snippet=_redact(m.group(0)), line_no=idx))

        # High-entropy assignment values that aren't placeholders.
        for m in _ASSIGNMENT_RE.finditer(added):
            value = m.group(1)
            if _PLACEHOLDER_RE.search(value):
                continue
            if len(value) < _MIN_VALUE_LEN:
                continue
            ent = _shannon_entropy(value)
            if ent >= _MIN_ENTROPY:
                key = ("high_entropy_assignment", idx)
                if key not in seen:
                    seen.add(key)
                    findings.append(
                        SecretFinding(
                            kind="high_entropy_assignment",
                            snippet=f"{value[:6]}…{value[-2:]} (entropy {ent:.1f})",
                            line_no=idx,
                            entropy=ent,
                        )
                    )

    return findings


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _shannon_entropy(s: str) -> float:
    """Bits per character. Higher → more random → more secret-like."""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_assigned(line: str, token: str) -> bool:
    """Heuristic: is the token in an assignment context, not a comment/doc?

    Returns True only if the line has ``KEY=`` or ``KEY: "..."`` shape
    (with the key before any comment marker like ``#`` or ``//``). This
    prevents flagging commit SHAs in comments or log messages.
    """
    # Strip inline comments first.
    stripped = line.split("#")[0].split("//")[0].strip()
    if "=" in stripped or stripped.endswith(":"):
        return True
    return ("'" in line or '"' in line) and token in line


def _redact(secret: str) -> str:
    """Show enough to identify the kind, never the whole secret."""
    if len(secret) <= 8:
        return secret[:2] + "…" + secret[-1:]
    return secret[:4] + "…" + secret[-2:]


# --------------------------------------------------------------------------- #
# v0.5.14: TruffleHog integration
# --------------------------------------------------------------------------- #


def trufflehog_installed() -> bool:
    """Return True if TruffleHog is on PATH."""
    return shutil.which("trufflehog") is not None


def scan_with_trufflehog(
    worktree_path: Path,
    *,
    timeout_seconds: int = 120,
) -> list[SecretFinding]:
    """Run TruffleHog on the worktree and return findings.

    TruffleHog performs **verified detection** — it checks if a detected
    secret is live (e.g., makes a test API call) before reporting it. This
    eliminates false positives that the regex scanner would produce.

    Uses ``trufflehog git file://<path> --json --no-update`` to scan the
    worktree's git history + working tree. Only verified findings are
    returned.

    **Limitation (v0.7.5):** TruffleHog only verifies its built-in detector
    patterns. Custom company regexes (e.g., ``IAK-...``) configured via
    ``review.custom_secret_regexes`` are NOT verified by TruffleHog — they
    are only checked by the raw regex scanner (:func:`scan_patch`). This
    means custom regex matches cannot be cryptographically verified as
    "live" keys. Custom matches are still hard-blocked by the regex scanner,
    but they may produce false positives that TruffleHog would not. This is
    a known limitation of the TruffleHog CLI — it does not accept custom
    detector definitions via command-line flags.

    Returns an empty list if TruffleHog finds nothing or is not installed.
    Raises RuntimeError on TruffleHog execution failure (not timeout).
    """
    if not trufflehog_installed():
        return []

    findings: list[SecretFinding] = []

    try:
        # v0.7.4: Use Path.as_uri() instead of f"file://{path}" to safely
        # URL-encode special characters in the path (spaces, #, etc.).
        # Without this, a worktree at /Users/dev/my#project/ would produce
        # file:///Users/dev/my#project/ which TruffleHog parses incorrectly
        # (the # is treated as a URI fragment), silently returning zero findings.
        # v0.7.4: Stream stdout to a temp file instead of capturing into
        # memory. TruffleHog on a large repo can produce hundreds of MB
        # of JSON output, which would cause OOM with capture_output=True.
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as stdout_f:
            stdout_tmp_path = Path(stdout_f.name)
        try:
            with open(stdout_tmp_path, "w") as stdout_f:
                subprocess.run(
                    [
                        "trufflehog",
                        "git",
                        worktree_path.resolve().as_uri(),
                        "--json",
                        "--no-update",
                        "--results=verified,unknown,unverified",
                    ],
                    stdout=stdout_f,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            stdout_tmp_path.unlink(missing_ok=True)
            return []
        except FileNotFoundError:
            stdout_tmp_path.unlink(missing_ok=True)
            return []
    except Exception:  # noqa: BLE001
        return []

    # TruffleHog outputs one JSON object per line on stdout.
    # v0.7.4: Read from the temp file instead of proc.stdout.
    try:
        for line in stdout_tmp_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # TruffleHog JSON format includes:
            # - DetectorName: the type of secret detected
            # - Verified: whether the secret was verified as live
            # - Raw: the raw secret value (we redact this)
            # - SourceMetadata: file path, line number
            # We only include verified findings to avoid false positives.
            detector = obj.get("DetectorName", "unknown")
            verified = obj.get("Verified", False)
            raw_secret = obj.get("Raw", "")
            source_meta = obj.get("SourceMetadata", {})
            line_no = 0

            # Extract line number from source metadata if available.
            metadata = source_meta.get("Metadata", {})
            if isinstance(metadata, dict):
                line_no = metadata.get("line", 0)

            # Only include verified findings — TruffleHog's key advantage.
            if not verified:
                continue

            kind = f"trufflehog:{detector.lower()}"
            snippet = _redact(raw_secret) if raw_secret else "(verified secret detected)"

            findings.append(
                SecretFinding(
                    kind=kind,
                    snippet=snippet,
                    line_no=line_no,
                )
            )
    finally:
        # v0.7.4: Clean up the temp file.
        stdout_tmp_path.unlink(missing_ok=True)

    return findings


def scan_diff(
    patch: str,
    *,
    worktree_path: Path | None = None,
    use_trufflehog: bool = True,
    custom_regexes: list[tuple[str, re.Pattern[str]]] | None = None,
) -> tuple[list[SecretFinding], dict[str, str]]:
    """Scan a diff for secrets, using TruffleHog if available.

    This is the v0.5.14 entry point for secret scanning. It uses
    TruffleHog for verified detection when available (and a worktree
    path is provided), falling back to the regex-based ``scan_patch``
    when TruffleHog is not installed or not requested.

    When TruffleHog is used, the regex scanner is also run as a
    complementary check — TruffleHog catches verified secrets but may
    miss unverified ones that the regex scanner would flag.

    v0.7.0 (Phase 3.2): When ``custom_regexes`` is provided, each
    (name, pattern) pair is checked in addition to the built-in patterns.
    Custom matches are tagged with kind ``custom:<name>`` and are
    included in hard-block detection.

    v0.7.3 (Phase 2.4): Returns a tuple ``(findings, scan_info)`` where
    ``scan_info`` is a dict with ``"scanner"`` (``"trufflehog+regex"``
    or ``"regex_only"``) and ``"degraded"`` (``"true"`` if TruffleHog
    was expected but not installed, ``"false"`` otherwise). This lets
    the review surface degradation in the report and event log.

    Backwards compatibility: callers that expect only a list will get a
    tuple — use ``scan_diff_compat()`` for the old list-only return.
    """
    # Always run the regex scanner — it catches patterns TruffleHog might
    # miss (e.g., private key blocks that TruffleHog doesn't verify).
    findings = scan_patch(patch, custom_regexes=custom_regexes)

    degraded = "false"
    scanner = "regex_only"

    # If TruffleHog is available and a worktree path is provided, run it
    # for verified detection. Merge any new findings.
    if use_trufflehog and worktree_path is not None:
        if trufflehog_installed():
            scanner = "trufflehog+regex"
            trufflehog_findings = scan_with_trufflehog(worktree_path)
            # Dedupe by (kind, line_no) — a finding from both scanners
            # should only appear once. Prefer the TruffleHog finding.
            existing_keys = {(f.kind, f.line_no) for f in findings}
            for tf in trufflehog_findings:
                key = (tf.kind, tf.line_no)
                if key not in existing_keys:
                    findings.append(tf)
        else:
            # TruffleHog was requested but is not installed — surface
            # this as a degradation so the operator knows the scan is
            # less thorough than configured.
            degraded = "true"
            logger.warning(
                "review.use_trufflehog=true but trufflehog is not installed — "
                "falling back to regex-only secret scanning. "
                "Install TruffleHog: https://github.com/trufflesecurity/trufflehog"
            )

    scan_info = {"scanner": scanner, "degraded": degraded}
    return findings, scan_info


def scan_diff_compat(
    patch: str,
    *,
    worktree_path: Path | None = None,
    use_trufflehog: bool = True,
    custom_regexes: list[tuple[str, re.Pattern[str]]] | None = None,
) -> list[SecretFinding]:
    """Backwards-compatible wrapper that returns only findings (no info dict)."""
    findings, _ = scan_diff(
        patch,
        worktree_path=worktree_path,
        use_trufflehog=use_trufflehog,
        custom_regexes=custom_regexes,
    )
    return findings


# --------------------------------------------------------------------------- #
# v0.7.0: Pre-commit style semantic scanning — hard-block before TruffleHog
# --------------------------------------------------------------------------- #


# Kinds that constitute an immediate hard block (vs. advisory findings).
# These are the provider-specific patterns (AWS keys, GitHub PATs, etc.)
# and private key blocks — they are always hard blocks regardless of
# whether TruffleHog verifies them as live.
_HARD_BLOCK_KINDS = frozenset(
    {
        "aws_access_key",
        "github_pat",
        "slack_token",
        "openai_key",
        "anthropic_key",
        "google_api_key",
        "stripe_key",
        "private_key_block",
        "jwt",
    }
)


def detect_hard_block_secrets(
    patch: str,
    *,
    custom_regexes: list[tuple[str, re.Pattern[str]]] | None = None,
) -> list[SecretFinding]:
    """Fast pre-TruffleHog scan for secrets that constitute a HARD_BLOCK.

    Runs only the regex scanner (no subprocess, no network) and returns
    only findings whose kind is in :data:`_HARD_BLOCK_KINDS`. This is
    used by the ``review_diff`` node to emit a ``review.secret_hard_block``
    event *before* the slower TruffleHog verified scan, failing the
    review immediately if a high-entropy or known-pattern secret is
    detected.

    High-entropy assignment findings are included when the entropy is
    particularly high (>= 4.0 bits/char), indicating a very likely secret.
    The threshold is higher than the scan threshold (3.5) to reduce false
    positives for the hard-block path.

    v0.7.0 (Phase 3.2): Custom regex matches (kind starting with
    ``custom:``) are always treated as hard blocks — the user explicitly
    defined them because they know they're secrets.
    """
    findings = scan_patch(patch, custom_regexes=custom_regexes)
    hard_blocks: list[SecretFinding] = []
    for f in findings:
        if f.kind in _HARD_BLOCK_KINDS:
            hard_blocks.append(f)
        elif f.kind.startswith("custom:"):
            # Custom regexes are user-defined — always hard-block.
            hard_blocks.append(f)
        elif f.kind == "high_entropy_assignment":
            # Use the entropy field directly (set during scan) instead of
            # parsing the snippet string, which is fragile.
            if f.entropy >= 4.0:
                hard_blocks.append(f)
    return hard_blocks
