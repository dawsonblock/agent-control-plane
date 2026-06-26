"""Skills governance (Milestone 8 — fusion-skills-style YAML playbooks).

Each major agent action (review, repair, memory promotion, worktree
safety) can be governed by a named YAML skill in ``skills/``. A skill
defines:

  - **Prompt instructions**: extra guidance injected into the agent
    prompt when the skill is active.
  - **Review gates**: dynamic review rules that augment the RiskEngine
    (hard blocks, risk elevators, required files).
  - **Rules**: human-readable rule descriptions for documentation.

Skill files can be:
  - ``.yaml`` files with the skill definition as a YAML mapping
  - ``SKILL.md`` files with YAML frontmatter + markdown body

Usage::

    from acp.skills import load_skills, apply_skill_review_gates

    skills = load_skills(Path("skills"))
    skill = skills.get("RefactorDatabase")
    if skill:
        apply_skill_review_gates(engine, skill, diff.changed_files)
"""

from acp.skills.enforcement import (
    apply_skill_review_gates,
    get_active_skill,
    get_skill_prompt_instructions,
)
from acp.skills.loader import (
    load_skill,
    load_skills,
    validate_skill,
)

__all__ = [
    "load_skills",
    "load_skill",
    "validate_skill",
    "apply_skill_review_gates",
    "get_active_skill",
    "get_skill_prompt_instructions",
]
