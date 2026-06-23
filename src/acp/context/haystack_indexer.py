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