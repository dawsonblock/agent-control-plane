"""Haystack indexer for the control plane.

This module provides functionality to index repository content for Haystack
retrieval. It scans the repository for relevant files and creates an index
that can be used to retrieve context for coding tasks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.config import ContextSection


class HaystackIndexer:
    """Index repository content for Haystack retrieval.

    This class scans a repository and creates an index of its content
    that can be used to retrieve relevant context for coding tasks.
    """

    def __init__(self, repo_path: Path, context_config: ContextSection) -> None:
        self.repo_path = repo_path
        self.context_config = context_config
        self.index: dict[str, Any] = {}

    def build_index(self) -> None:
        """Build the index of repository content.

        This method scans the repository and creates an index of its content
        based on the context configuration.
        """
        self.index = {
            "repo_path": str(self.repo_path),
            "context_config": self.context_config.model_dump(),
            "files": [],
            "metadata": {},
        }

        # Scan for files to include
        from acp.context.scanner import scan_context

        for file_path in scan_context(self.repo_path, self.context_config):
            self.index["files"].append({
                "path": str(file_path.relative_to(self.repo_path)),
                "size": file_path.stat().st_size,
                "modified": file_path.stat().st_mtime,
            })

        # Add metadata
        self.index["metadata"] = {
            "total_files": len(self.index["files"]),
            "total_size": sum(f["size"] for f in self.index["files"]),
            "last_modified": max(
                f["modified"] for f in self.index["files"]
            )
            if self.index["files"]
            else 0,
        }

    def get_file_content(self, file_path: str) -> str:
        """Get the content of a file from the index.

        Args:
            file_path: Path to the file relative to the repository root.

        Returns:
            The content of the file as a string.

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