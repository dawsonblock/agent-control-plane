"""Shared I/O helpers — deduplicate safe file reads and event writes.

Several modules repeat the same patterns:
  - Read a file, return None on failure (best-effort reads).
  - Write an event, log and continue on failure (best-effort evidence).

These helpers centralize those patterns so error handling is consistent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def safe_read_text(path: Path, *, encoding: str = "utf-8") -> str | None:
    """Read a text file, returning ``None`` on any I/O or decode error.

    Use for best-effort reads where a missing/unreadable file is a
    degraded-but-acceptable state (e.g., reading an optional config file).
    The failure is logged at ``warning`` level so degradation is visible.
    """
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("safe_read_text: failed to read %s: %s", path, exc)
        return None


def safe_read_json(path: Path) -> Any | None:
    """Read and parse a JSON file, returning ``None`` on any error.

    Combines :func:`safe_read_text` with ``json.loads``. Returns ``None``
    if the file is missing, unreadable, or contains invalid JSON.
    """
    text = safe_read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("safe_read_json: invalid JSON in %s: %s", path, exc)
        return None


def safe_read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file (one JSON object per line), skipping blank/bad lines.

    Returns a list of parsed dicts. Blank lines are silently skipped.
    Malformed lines are logged at ``warning`` level and skipped (not fatal).
    If the file doesn't exist, returns an empty list.
    """
    if not path.is_file():
        return []
    results: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("safe_read_jsonl: bad line in %s: %s", path, exc)
    return results
