"""Stream-specific secret scanner — detects credentials in raw agent output.

Unlike :func:`acp.review.secret_scanner.scan_patch`, which operates on
unified-diff lines (``+``-prefixed added lines with assignment context),
this scanner operates on raw text streams — the agent's stdout as it's
emitted, line by line. This is the kill-switch layer of the
:class:`~acp.streaming.midstream.StreamSentinel`.

The scanner reuses the same provider regex patterns (AWS, GitHub, Slack,
OpenAI, etc.) from :mod:`acp.review.secret_scanner` but drops the
``+``-prefix and assignment-context requirements — provider patterns
(AKIA..., ghp_..., xox...-, sk-...) are distinctive enough to match
anywhere in raw text without diff-line structure.

High-entropy assignment detection is retained but adapted: in a stream,
the agent might print ``API_KEY = "wJalrXUtnFEMI..."`` in prose or a
config snippet, not just in a diff line. The same entropy threshold
(>= 3.5 bits/char) and placeholder exclusion apply.

Custom regexes (from ``review.custom_secret_regexes``) are also checked,
tagged with kind ``custom:<name>`` — same as the diff scanner.
"""

from __future__ import annotations

import re

from acp.review.secret_scanner import (
    _PLACEHOLDER_RE,
    _PROVIDER_PATTERNS,
    _shannon_entropy,
)

# Assignment-shaped lines: KEY = "..." / KEY: '...' / export KEY=...
# Same regex as the diff scanner — works on any text line, not just diffs.
_ASSIGNMENT_RE = re.compile(
    r"(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
)

# Minimum value length for entropy-based detection (same as diff scanner).
_MIN_VALUE_LEN = 20
_MIN_ENTROPY = 3.5


class StreamSecretFinding:
    """A secret-like string detected in the agent's output stream.

    Unlike :class:`acp.review.secret_scanner.SecretFinding`, this does not
    carry a ``line_no`` (the stream is processed incrementally, not as a
    complete patch). The ``chunk_index`` tracks which chunk was being
    analyzed when the finding was made.
    """

    __slots__ = ("kind", "snippet", "chunk_index", "entropy")

    def __init__(
        self,
        kind: str,
        snippet: str,
        chunk_index: int = 0,
        entropy: float = 0.0,
    ) -> None:
        self.kind = kind
        self.snippet = snippet
        self.chunk_index = chunk_index
        self.entropy = entropy

    def __repr__(self) -> str:
        return f"StreamSecretFinding(kind={self.kind!r}, snippet={self.snippet!r})"


def scan_stream(
    text: str,
    *,
    chunk_index: int = 0,
    custom_regexes: list[tuple[str, re.Pattern[str]]] | None = None,
) -> list[StreamSecretFinding]:
    """Scan a raw text chunk for secret-like content.

    This is the stream equivalent of :func:`acp.review.secret_scanner.scan_patch`.
    It checks provider patterns (AWS, GitHub, Slack, etc.) against the full
    text (no ``+``-prefix requirement) and runs high-entropy detection on
    assignment-shaped substrings.

    Args:
        text: The raw text chunk from the agent's stdout.
        chunk_index: The index of this chunk in the stream (for reporting).
        custom_regexes: Optional (name, pattern) pairs from the repo config's
            ``review.custom_secret_regexes``. Matches are tagged
            ``custom:<name>``.

    Returns:
        A list of :class:`StreamSecretFinding` objects. Empty if no secrets
        were detected.
    """
    findings: list[StreamSecretFinding] = []
    seen_kinds: set[str] = set()

    # Merge built-in and custom patterns.
    all_patterns = list(_PROVIDER_PATTERNS)
    if custom_regexes:
        all_patterns.extend(custom_regexes)

    # Check provider patterns against the full text (no diff-line gate).
    for kind, pat in all_patterns:
        for m in pat.finditer(text):
            if kind in seen_kinds:
                continue
            # Guard the 40-hex legacy-token rule: in a stream, a bare 40-hex
            # string is very likely a commit SHA, not a secret. Only flag it
            # if it appears in an assignment or quoted-string context.
            if kind == "github_legacy_token":
                if not _looks_assigned_or_quoted(text, m.group(0)):
                    continue
            seen_kinds.add(kind)
            findings.append(
                StreamSecretFinding(
                    kind=kind,
                    snippet=_redact(m.group(0)),
                    chunk_index=chunk_index,
                )
            )

    # High-entropy assignment values (same logic as the diff scanner).
    for m in _ASSIGNMENT_RE.finditer(text):
        value = m.group(1)
        if _PLACEHOLDER_RE.search(value):
            continue
        if len(value) < _MIN_VALUE_LEN:
            continue
        ent = _shannon_entropy(value)
        if ent >= _MIN_ENTROPY:
            kind = "high_entropy_assignment"
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            findings.append(
                StreamSecretFinding(
                    kind=kind,
                    snippet=f"{value[:6]}…{value[-2:]} (entropy {ent:.1f})",
                    chunk_index=chunk_index,
                    entropy=ent,
                )
            )

    return findings


def _looks_assigned_or_quoted(text: str, token: str) -> bool:
    """Heuristic: is the token in an assignment or quoted context?

    In a raw stream (not a diff), a bare 40-hex string is almost certainly
    a commit SHA in a log line. Only flag it if it's inside quotes or after
    an ``=`` assignment operator. We do NOT treat ``Word:`` as assignment
    context (unlike the diff scanner) because stream output contains prose
    like ``Commit: <sha>`` or ``Error: <hash>`` where the colon is natural
    language, not an assignment.
    """
    idx = text.find(token)
    if idx < 0:
        return False
    before = text[:idx].rstrip()
    after = text[idx + len(token) :]
    # Assignment context: KEY=token (strict — only =, not :)
    if before.endswith("="):
        return True
    # Quoted context: "token" or 'token'
    if after and after[0] in "\"'":
        return True
    if before and before[-1] in "\"'":
        return True
    return False


def _redact(secret: str) -> str:
    """Show enough to identify the kind, never the whole secret."""
    if len(secret) <= 8:
        return secret[:2] + "…" + secret[-1:]
    return secret[:4] + "…" + secret[-2:]
