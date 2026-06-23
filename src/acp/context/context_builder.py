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