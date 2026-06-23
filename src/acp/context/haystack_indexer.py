"""Haystack indexer — deferred until M6.

M6 will implement real Haystack retrieval for building context bundles
before agent runs. Until then, this module raises ``NotImplementedError``
to prevent accidental use. See docs/roadmap.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.config import ContextSection


class HaystackIndexer:
    """Index repository content for Haystack retrieval.

    Raises:
        NotImplementedError: Always — M6 deferred. See docs/roadmap.md.
    """

    def __init__(self, repo_path: Path, context_config: ContextSection) -> None:
        raise NotImplementedError(
            "Haystack indexing is not implemented until M6. "
            "See docs/roadmap.md."
        )

    def build_index(self) -> None:
        raise NotImplementedError(
            "Haystack indexing is not implemented until M6. "
            "See docs/roadmap.md."
        )

    def retrieve_context(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Haystack retrieval is not implemented until M6. "
            "See docs/roadmap.md."
        )

        Raises:
            FileNotFoundError: If the file is not in the index.
        """
        for file_info in self.index["files"]:
            if file_info["path"] == file_path:
                return Path(self.repo_path / file_path).read_text()

        raise FileNotFoundError(f"File not found in index: {file_path}")

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search for files in the index that match a query.

        Args:
            query: The search query.

        Returns:
            A list of file information dictionaries that match the query.
        """
        results = []
        for file_info in self.index["files"]:
            if query.lower() in file_info["path"].lower():
                results.append(file_info)

        return results