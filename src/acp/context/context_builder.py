"""Context builder — deferred until M6.

M6 will implement real Haystack-based context bundles. Until then,
``ContextBuilder`` raises ``NotImplementedError`` to prevent accidental
use of stub infrastructure. See docs/roadmap.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.config import ContextSection


class ContextBuilder:
    """Build context bundles for coding tasks.

    Raises:
        NotImplementedError: Always — M6 deferred. See docs/roadmap.md.
    """

    def __init__(self, repo_path: Path, context_config: ContextSection) -> None:
        raise NotImplementedError(
            "Context building is not implemented until M6. "
            "See docs/roadmap.md."
        )

    def build_context_bundle(self) -> dict[str, Any]:
        raise NotImplementedError(
            "Context building is not implemented until M6. "
            "See docs/roadmap.md."
        )

    def get_relevant_files(self, task_description: str) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Context building is not implemented until M6. "
            "See docs/roadmap.md."
        )
                keyword in file_path for keyword in task_keywords
            )

            if is_relevant:
                relevant_files.append(file_info)

        return relevant_files

    def filter_files_by_patterns(
        self, file_patterns: list[str]
    ) -> list[dict[str, Any]]:
        """Filter files by patterns.

        Args:
            file_patterns: List of file patterns to match.

        Returns:
            A list of file information dictionaries that match the patterns.
        """
        import fnmatch

        filtered_files = []
        for file_info in self.indexer.index["files"]:
            file_path = file_info["path"]
            for pattern in file_patterns:
                if fnmatch.fnmatch(file_path, pattern):
                    filtered_files.append(file_info)
                    break

        return filtered_files