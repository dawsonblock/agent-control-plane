"""Skill loader for fusion-skills governance.

Loads and validates YAML skill definitions from the skills directory.
Each skill defines governance rules for a specific aspect of the workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from acp.errors import SkillLoadError


def load_skills(skills_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all skill definitions from a directory.

    Args:
        skills_dir: Path to the skills directory containing YAML files.

    Returns:
        Dictionary mapping skill names to their definitions.

    Raises:
        SkillLoadError: If a skill file cannot be loaded or parsed.
    """
    skills = {}
    for skill_file in skills_dir.glob("*.yaml"):
        try:
            with open(skill_file, "r") as f:
                skill_def = yaml.safe_load(f)

            # Validate required fields
            if "name" not in skill_def:
                raise SkillLoadError(f"Skill file {skill_file} missing 'name' field")

            skill_name = skill_def["name"]
            skills[skill_name] = skill_def

        except yaml.YAMLError as e:
            raise SkillLoadError(f"Invalid YAML in skill file {skill_file}: {e}")
        except Exception as e:
            raise SkillLoadError(f"Failed to load skill file {skill_file}: {e}")

    return skills


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