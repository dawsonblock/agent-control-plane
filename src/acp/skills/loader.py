"""Skill loader (M8) — fusion-skills-style YAML playbooks.

Loads skill definitions from ``SKILL.md`` or ``.yaml`` files in a skills
directory. Each skill is a YAML playbook that governs how the control
plane handles a specific type of task:

  - **Prompt instructions**: extra instructions injected into the agent
    prompt when the skill is active.
  - **Review gates**: dynamic review rules that augment the RiskEngine.
    For example, a ``RefactorDatabase`` skill can hard-block any schema
    deletion without a corresponding backup file.
  - **Required files**: file patterns that MUST appear in the diff (e.g.,
    a ``RefactorDatabase`` skill might require a migration file).

Skill files use the following schema (YAML frontmatter in SKILL.md or
a plain ``.yaml`` file)::

    name: RefactorDatabase
    purpose: Safely refactor database schema with backup and migration
    prompt_instructions: |
      When refactoring database schema:
      - Always create a backup before deleting columns
      - Include both up and down migrations
      - Test the migration on a copy of the schema first
    review_gates:
      hard_blocks:
        - description: "Block schema deletion without backup file"
          file_pattern: "*migration*drop*"
          requires_file: "*backup*"
      risk_elevators:
        - description: "Elevate risk for raw SQL changes"
          file_pattern: "*.sql"
          to_level: high
      required_files:
        - "*migration*"
    rules:
      - "Never delete a column without a backup"
      - "Always include up and down migrations"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from acp.errors import SkillLoadError

# --------------------------------------------------------------------------- #
# Skill schema (validated by Pydantic in the caller)
# --------------------------------------------------------------------------- #

REQUIRED_FIELDS = ("name", "purpose", "rules", "hard_blocks")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load_skills(skills_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all skill definitions from a directory.

    Scans for ``*.yaml`` files and ``SKILL.md`` files (with YAML
    frontmatter). Returns a dict mapping skill name → skill definition.

    Args:
        skills_dir: Directory containing skill definition files.

    Returns:
        Dict of ``{skill_name: skill_definition}``. Empty if the
        directory doesn't exist or contains no valid skills.

    Raises:
        SkillLoadError: If a skill file exists but is malformed.
    """
    if not skills_dir.is_dir():
        return {}

    skills: dict[str, dict[str, Any]] = {}

    # Load .yaml files.
    for yaml_file in sorted(skills_dir.glob("*.yaml")):
        try:
            skill = _load_yaml_file(yaml_file)
        except Exception as exc:  # noqa: BLE001
            raise SkillLoadError(f"Failed to load skill from {yaml_file}: {exc}") from exc
        if skill and validate_skill(skill):
            skills[skill["name"]] = skill

    # Load SKILL.md files (YAML frontmatter + markdown body).
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        try:
            skill = _load_skill_md(skill_md)
        except Exception as exc:  # noqa: BLE001
            raise SkillLoadError(f"Failed to load skill from {skill_md}: {exc}") from exc
        if skill and validate_skill(skill):
            skills[skill["name"]] = skill

    return skills


def load_skill(skill_path: Path) -> dict[str, Any]:
    """Load a single skill definition from a file.

    Args:
        skill_path: Path to a ``.yaml`` or ``SKILL.md`` file.

    Returns:
        The skill definition as a dict.

    Raises:
        SkillLoadError: If the file is malformed or validation fails.
        FileNotFoundError: If the file doesn't exist.
    """
    if not skill_path.is_file():
        raise FileNotFoundError(f"skill file not found: {skill_path}")

    try:
        if skill_path.name == "SKILL.md":
            skill = _load_skill_md(skill_path)
        else:
            skill = _load_yaml_file(skill_path)
    except Exception as exc:  # noqa: BLE001
        raise SkillLoadError(f"Failed to load skill from {skill_path}: {exc}") from exc

    if not validate_skill(skill):
        raise SkillLoadError(
            f"Skill validation failed for {skill_path}: missing required fields {REQUIRED_FIELDS}"
        )

    return skill


def validate_skill(skill: dict[str, Any]) -> bool:
    """Validate a skill definition.

    Checks that all required fields are present and have the correct types.

    Args:
        skill: The skill definition to validate.

    Returns:
        True if the skill is valid, False otherwise.
    """
    for field in REQUIRED_FIELDS:
        if field not in skill:
            return False

    if not isinstance(skill["name"], str):
        return False
    if not isinstance(skill["purpose"], str):
        return False
    if not isinstance(skill["rules"], list):
        return False
    if not isinstance(skill["hard_blocks"], list):
        return False

    # Validate review_gates if present.
    gates = skill.get("review_gates", {})
    if not isinstance(gates, dict):
        return False

    hard_blocks = gates.get("hard_blocks", [])
    if not isinstance(hard_blocks, list):
        return False
    for hb in hard_blocks:
        if not isinstance(hb, dict):
            return False
        if "description" not in hb:
            return False

    risk_elevators = gates.get("risk_elevators", [])
    if not isinstance(risk_elevators, list):
        return False
    for re_ in risk_elevators:
        if not isinstance(re_, dict):
            return False
        if "description" not in re_:
            return False

    required_files = gates.get("required_files", [])
    if not isinstance(required_files, list):
        return False

    return True


# --------------------------------------------------------------------------- #
# Internal: file parsers
# --------------------------------------------------------------------------- #


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a plain YAML skill file."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"skill file must be a YAML mapping: {path}")
    return data


def _load_skill_md(path: Path) -> dict[str, Any]:
    """Load a SKILL.md file with YAML frontmatter.

    The file must start with ``---`` and contain a YAML block. The
    markdown body after the frontmatter is stored as ``body`` in the
    skill dict.
    """
    content = path.read_text(encoding="utf-8")
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        raise ValueError(f"SKILL.md must start with YAML frontmatter: {path}")

    parts = stripped[3:].split("---", 1)
    if len(parts) < 2:
        raise ValueError(f"Missing closing '---' in frontmatter: {path}")

    yaml_text = parts[0].strip()
    body = parts[1].strip()

    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        raise ValueError(f"Frontmatter must be a YAML mapping: {path}")

    # Store the markdown body as an extra field.
    data["body"] = body
    return data
