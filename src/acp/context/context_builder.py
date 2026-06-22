"""Context builder for the control plane.

This module provides functionality to build context bundles for coding tasks.
It uses the Haystack indexer to retrieve relevant files and creates a
context bundle that can be provided to agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.config import ContextSection
from acp.context.haystack_indexer import HaystackIndexer


class ContextBuilder:
    """Build context bundles for coding tasks.

    This class uses the Haystack indexer to retrieve relevant files and
    creates a context bundle that can be provided to agents.
    """

    def __init__(self, repo_path: Path, context_config: ContextSection) -> None:
        self.repo_path = repo_path
        self.context_config = context_config
        self.indexer = HaystackIndexer(repo_path, context_config)

    def build_context_bundle(self) -> dict[str, Any]:
        """Build a context bundle for a coding task.

        Returns:
            A dictionary containing the context bundle.
        """
        # Build the index
        self.indexer.build_index()

        # Create the context bundle
        context_bundle = {
            "repo_path": str(self.repo_path),
            "context_config": self.context_config.model_dump(),
            "index": self.indexer.index,
            "files": [],
        }

        # Add file contents to the context bundle
        for file_info in self.indexer.index["files"]:
            file_path = Path(self.repo_path) / file_info["path"]
            try:
                content = file_path.read_text()
                context_bundle["files"].append({
                    "path": file_info["path"],
                    "content": content,
                    "size": file_info["size"],
                    "modified": file_info["modified"],
                })
            except Exception as e:
                # Skip files that can't be read
                continue

        return context_bundle

    def get_relevant_files(self, task_description: str) -> list[dict[str, Any]]:
        """Get files that are relevant to a task description.

        Args:
            task_description: Description of the task to perform.

        Returns:
            A list of file information dictionaries that are relevant to the task.
        """
        # Simple relevance check based on file path
        relevant_files = []
        for file_info in self.indexer.index["files"]:
            # Check if the file path contains keywords from the task description
            file_path = file_info["path"].lower()
            task_keywords = task_description.lower().split()

            # Simple relevance check
            is_relevant = any(
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