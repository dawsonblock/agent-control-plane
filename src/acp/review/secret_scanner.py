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
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# --- known credential prefixes / shapes ------------------------------------ #
# Each entry: (label, compiled regex). Matched against added lines.
_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # AWS secret: a 40-char base64-ish value on a line whose key mentions
    # secret/priv (catches AWS_SECRET_ACCESS_KEY, aws_secret, privateKey...).
    ("aws_secret", re.compile(r"(?i)^(?=[^\n]*(?:secret|priv)).{0,40}[=:]\s*['\"]?[A-Za-z0-9/+=]{40}(?:['\"]|\s|$)")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_legacy_token", re.compile(r"\b[a-f0-9]{40}\b")),  # 40-hex legacy; gated below
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # OpenAI: allow a hyphenated project prefix like sk-proj-...
    ("openai_key", re.compile(r"\bsk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{24,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
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
_MIN_ENTROPY = 3.5      # bits/char; real secrets cluster 3.5–6
_MIN_VALUE_LEN = 20     # ignore short assignment values
_MIN_VOWEL_FRACTION = 0.0  # informational; kept for tuning


@dataclass
class SecretFinding:
    """One detected secret. The reviewer turns this into a hard-block signal."""

    kind: str          # provider label or "high_entropy_assignment"
    snippet: str       # redacted excerpt for the report (never the full secret)
    line_no: int       # line in the patch where it appeared


def scan_patch(patch: str) -> list[SecretFinding]:
    """Scan added lines of a unified diff patch for secret-like content."""
    findings: list[SecretFinding] = []
    seen: set[tuple[str, int]] = set()  # dedupe (kind, line_no)

    for idx, raw_line in enumerate(patch.splitlines(), start=1):
        # Only added lines carry new secret risk.
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        added = raw_line[1:]

        for kind, pat in _PROVIDER_PATTERNS:
            for m in pat.finditer(added):
                key = (kind, idx)
                if key in seen:
                    continue
                # Guard the 40-hex legacy-token rule against commit SHAs in
                # comments: require the match to be inside a string or assignment.
                if kind == "github_legacy_token" and not _looks_assigned(added, m.group(0)):
                    continue
                seen.add(key)
                findings.append(
                    SecretFinding(kind=kind, snippet=_redact(m.group(0)), line_no=idx)
                )

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
    """Heuristic: is the token inside a string literal or an assignment value?"""
    if "=" in line or ":" in line:
        return True
    return ("'" in line or '"' in line) and token in line


def _redact(secret: str) -> str:
    """Show enough to identify the kind, never the whole secret."""
    if len(secret) <= 8:
        return secret[:2] + "…" + secret[-1:]
    return secret[:4] + "…" + secret[-2:]
