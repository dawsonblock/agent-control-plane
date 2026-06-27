"""Skill enforcement (M8) — apply skill review gates to the RiskEngine.

When a skill is active for a task, its review gates are applied on top
of the default risk evaluation. This module provides the functions that
the graph nodes call to:

  1. Inject skill prompt instructions into the agent prompt.
  2. Apply skill review gates (hard blocks, risk elevators, required
     files) to the RiskEngine after the default evaluation.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from acp.models import RiskLevel
from acp.review.risk import RiskCategory, RiskEngine
from acp.skills.loader import load_skills

# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def get_skill_prompt_instructions(
    skill_name: str,
    skills_dir: Path | None = None,
) -> str:
    """Get the prompt instructions for a skill.

    Returns the skill's ``prompt_instructions`` field, or an empty
    string if the skill doesn't exist or has no prompt instructions.

    Args:
        skill_name: The name of the skill (e.g., "RefactorDatabase").
        skills_dir: Directory containing skill definitions. If None,
            returns an empty string.

    Returns:
        The prompt instructions string (may be multi-line).
    """
    if skills_dir is None or not skills_dir.is_dir():
        return ""

    skills = load_skills(skills_dir)
    skill = skills.get(skill_name)
    if skill is None:
        return ""

    return str(skill.get("prompt_instructions", ""))


def apply_skill_review_gates(
    engine: RiskEngine,
    skill: dict[str, Any],
    changed_files: list[str],
) -> None:
    """Apply a skill's review gates to the RiskEngine.

    This is called AFTER the default risk evaluation in
    ``review_diff_node``. It adds extra signals to the engine based on
    the skill's rules:

      - **hard_blocks**: If a file matches ``file_pattern`` and the
        ``requires_file`` pattern is NOT found in the diff, add a
        hard-block signal.
      - **risk_elevators**: If a file matches ``file_pattern``, elevate
        the risk to ``to_level``.
      - **required_files**: If none of the changed files match the
        pattern, add a medium-risk signal.

    Args:
        engine: The RiskEngine to add signals to.
        skill: The skill definition dict.
        changed_files: List of file paths changed in the diff.
    """
    gates = skill.get("review_gates", {})
    changed_lower = [f.lower() for f in changed_files]

    # 1. Hard blocks: file_pattern present but requires_file missing.
    for hb in gates.get("hard_blocks", []):
        pattern = hb.get("file_pattern", "")
        requires = hb.get("requires_file", "")
        description = hb.get("description", "Skill hard block")

        if pattern and _any_match(changed_lower, pattern):
            if requires and not _any_match(changed_lower, requires):
                engine.add(
                    RiskCategory.DATABASE,  # reuse a relevant category
                    f"Skill hard block: {description} "
                    f"(matched '{pattern}' but missing '{requires}')",
                    level=RiskLevel.HIGH,
                    hard_block=True,
                )

    # 2. Risk elevators: file_pattern present → elevate risk.
    for re_ in gates.get("risk_elevators", []):
        pattern = re_.get("file_pattern", "")
        to_level_str = re_.get("to_level", "medium").lower()
        description = re_.get("description", "Skill risk elevator")

        if pattern and _any_match(changed_lower, pattern):
            try:
                to_level = RiskLevel(to_level_str)
            except ValueError:
                to_level = RiskLevel.MEDIUM
            engine.add(
                RiskCategory.DATABASE,
                f"Skill risk elevator: {description} (matched '{pattern}' → {to_level.value})",
                level=to_level,
            )

    # 3. Required files: at least one file must match.
    for req_pattern in gates.get("required_files", []):
        if not _any_match(changed_lower, req_pattern):
            engine.add(
                RiskCategory.TESTS_MISSING,  # closest category
                f"Skill required file missing: no file matched '{req_pattern}'",
                level=RiskLevel.MEDIUM,
            )


def get_active_skill(
    skill_name: str,
    skills_dir: Path | None,
) -> dict[str, Any] | None:
    """Get the active skill definition, or None if not found.

    Args:
        skill_name: The skill name to look up.
        skills_dir: Directory containing skill definitions.

    Returns:
        The skill dict, or None if not found / skills_dir is None.
    """
    if skills_dir is None or not skills_dir.is_dir():
        return None

    skills = load_skills(skills_dir)
    return skills.get(skill_name)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _any_match(file_paths: list[str], pattern: str) -> bool:
    """Check if any file path matches a glob pattern (case-insensitive)."""
    pattern_lower = pattern.lower()
    return any(fnmatch.fnmatch(fp, pattern_lower) or pattern_lower in fp for fp in file_paths)
