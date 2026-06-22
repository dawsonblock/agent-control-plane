"""Context scanner for the control plane.

This module provides functionality to scan a repository and identify relevant
files for context retrieval. It uses glob patterns to include/exclude files
and directories based on the repository configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

from acp.config import ContextSection


def scan_context(
    repo_path: Path,
    context_config: ContextSection,
) -> Generator[Path, None, None]:
    """Scan a repository for files to include in context bundles.

    Args:
        repo_path: Path to the repository root.
        context_config: Context configuration with include/exclude patterns.

    Yields:
        Path: Path to each file that should be included in the context bundle.
    """
    # Convert include/exclude patterns to Path objects
    include_patterns = [Path(p) for p in context_config.include]
    exclude_patterns = [Path(p) for p in context_config.exclude]

    # Walk through the repository
    for file_path in repo_path.rglob("*"):
        # Skip directories
        if file_path.is_dir():
            continue

        # Check if file matches include patterns
        should_include = False
        for pattern in include_patterns:
            if _matches_pattern(file_path, pattern, repo_path):
                should_include = True
                break

        # If no include patterns, include all files
        if not include_patterns:
            should_include = True

        # Skip if doesn't match include patterns
        if not should_include:
            continue

        # Check if file matches exclude patterns
        for pattern in exclude_patterns:
            if _matches_pattern(file_path, pattern, repo_path):
                should_include = False
                break

        if should_include:
            yield file_path


def _matches_pattern(file_path: Path, pattern: Path, repo_path: Path) -> bool:
    """Check if a file path matches a pattern.

    Args:
        file_path: The file path to check.
        pattern: The pattern to match against.
        repo_path: The repository root path.

    Returns:
        bool: True if the file matches the pattern.
    """
    # Convert to relative path from repo root
    try:
        rel_path = file_path.relative_to(repo_path)
    except ValueError:
        return False

    # Check if pattern is a glob pattern
    if "*" in str(pattern) or "?" in str(pattern) or "[" in str(pattern):
        # Use fnmatch for glob pattern matching
        import fnmatch

        return fnmatch.fnmatch(str(rel_path), str(pattern))
    else:
        # Exact match
        return rel_path == pattern or rel_path.name == pattern.name