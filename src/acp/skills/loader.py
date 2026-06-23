"""Skill loader — deferred until M8.

M8 will implement fusion-skills-style YAML playbooks that govern review,
repair, and memory-promotion behaviour. Until then, ``load_skills`` returns
an empty dict and all other functions raise ``NotImplementedError``.

See docs/roadmap.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.errors import SkillLoadError


def load_skills(skills_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all skill definitions from a directory.

    Returns an empty dict until M8. The SkillLoadError type is defined for
    forward compatibility.
    """
    return {}


def validate_skill(skill: dict[str, Any]) -> bool:
    """Validate a skill definition.

    Args:
        skill: The skill definition to validate.

    Returns:
        True if the skill is valid, False otherwise.
    """
    required_fields = ["name", "purpose", "rules", "hard_blocks"]
    for field in required_fields:
        if field not in skill:
            return False

    # Validate field types
    if not isinstance(skill["name"], str):
        return False
    if not isinstance(skill["purpose"], str):
        return False
    if not isinstance(skill["rules"], list):
        return False
    if not isinstance(skill["hard_blocks"], list):
        return False

    return True