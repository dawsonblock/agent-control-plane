"""M8 Skills governance tests.

Tests the skills governance feature:

  1. Skill loader — load_skills, load_skill, validate_skill
  2. Skill enforcement — apply_skill_review_gates, get_skill_prompt_instructions
  3. Prompt injection — write_prompt includes skill instructions
  4. Review gate integration — review_diff applies skill gates
  5. Config — SkillsSection parsing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.skills.loader import (
    load_skill,
    load_skills,
    validate_skill,
)
from acp.skills.enforcement import (
    apply_skill_review_gates,
    get_active_skill,
    get_skill_prompt_instructions,
)
from acp.review.risk import RiskEngine, RiskLevel
from acp.models import RiskLevel as ModelRiskLevel


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_skill(
    name: str = "TestSkill",
    purpose: str = "Test purpose",
    rules: list[str] | None = None,
    hard_blocks: list | None = None,
    review_gates: dict | None = None,
    prompt_instructions: str = "",
) -> dict:
    return {
        "name": name,
        "purpose": purpose,
        "rules": rules or ["rule 1", "rule 2"],
        "hard_blocks": hard_blocks or [],
        "review_gates": review_gates or {},
        "prompt_instructions": prompt_instructions,
    }


def _write_skill_yaml(
    skills_dir: Path,
    name: str,
    skill: dict,
) -> Path:
    """Write a skill as a .yaml file."""
    import yaml
    skills_dir.mkdir(parents=True, exist_ok=True)
    path = skills_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(skill, sort_keys=False))
    return path


# --------------------------------------------------------------------------- #
# 1. Skill loader
# --------------------------------------------------------------------------- #


class TestLoadSkills:
    """load_skills — load all skills from a directory."""

    def test_load_from_yaml_files(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "SkillA", _make_skill("SkillA"))
        _write_skill_yaml(skills_dir, "SkillB", _make_skill("SkillB"))

        skills = load_skills(skills_dir)
        assert set(skills.keys()) == {"SkillA", "SkillB"}
        assert skills["SkillA"]["name"] == "SkillA"
        assert skills["SkillB"]["name"] == "SkillB"

    def test_empty_dir_returns_empty(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        assert load_skills(skills_dir) == {}

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        assert load_skills(tmp_path / "nonexistent") == {}

    def test_invalid_yaml_skipped(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        # Write an invalid YAML file (not a mapping)
        (skills_dir / "bad.yaml").write_text("- just\n- a\n- list\n")
        # Should raise SkillLoadError
        from acp.errors import SkillLoadError
        with pytest.raises(SkillLoadError):
            load_skills(skills_dir)

    def test_load_skill_md(self, tmp_path):
        skills_dir = tmp_path / "skills" / "RefactorDB"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\n"
            "name: RefactorDB\n"
            "purpose: Refactor database safely\n"
            "rules:\n"
            "  - Always backup\n"
            "hard_blocks: []\n"
            "prompt_instructions: \"Create backups first\"\n"
            "---\n\n"
            "# RefactorDB Skill\n\nDetailed docs here.\n"
        )

        skills = load_skills(skills_dir.parent)
        assert "RefactorDB" in skills
        assert skills["RefactorDB"]["body"].startswith("# RefactorDB")

    def test_load_single_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        path = _write_skill_yaml(skills_dir, "SingleSkill", _make_skill("SingleSkill"))

        skill = load_skill(path)
        assert skill["name"] == "SingleSkill"

    def test_load_skill_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_skill(tmp_path / "nonexistent.yaml")


class TestValidateSkill:
    """validate_skill — schema validation."""

    def test_valid_skill(self):
        skill = _make_skill()
        assert validate_skill(skill) is True

    def test_missing_name(self):
        skill = _make_skill()
        del skill["name"]
        assert validate_skill(skill) is False

    def test_missing_purpose(self):
        skill = _make_skill()
        del skill["purpose"]
        assert validate_skill(skill) is False

    def test_missing_rules(self):
        skill = _make_skill()
        del skill["rules"]
        assert validate_skill(skill) is False

    def test_missing_hard_blocks(self):
        skill = _make_skill()
        del skill["hard_blocks"]
        assert validate_skill(skill) is False

    def test_wrong_type_name(self):
        skill = _make_skill()
        skill["name"] = 123
        assert validate_skill(skill) is False

    def test_wrong_type_rules(self):
        skill = _make_skill()
        skill["rules"] = "not a list"
        assert validate_skill(skill) is False

    def test_invalid_review_gates_type(self):
        skill = _make_skill()
        skill["review_gates"] = "not a dict"
        assert validate_skill(skill) is False

    def test_invalid_hard_block_entry(self):
        skill = _make_skill()
        skill["review_gates"] = {
            "hard_blocks": ["not a dict"],
        }
        assert validate_skill(skill) is False

    def test_hard_block_missing_description(self):
        skill = _make_skill()
        skill["review_gates"] = {
            "hard_blocks": [{"file_pattern": "*.sql"}],  # no description
        }
        assert validate_skill(skill) is False


# --------------------------------------------------------------------------- #
# 2. Skill enforcement
# --------------------------------------------------------------------------- #


class TestApplySkillReviewGates:
    """apply_skill_review_gates — apply skill rules to RiskEngine."""

    def test_hard_block_triggers_when_requires_missing(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "hard_blocks": [{
                "description": "Block drop without backup",
                "file_pattern": "*drop*",
                "requires_file": "*backup*",
            }],
        })
        # Diff has a "drop" file but no "backup" file
        apply_skill_review_gates(engine, skill, ["migrations/drop_column.sql"])

        assert engine.hard_block is True
        assert any("hard block" in c.lower() for c in engine.concerns)

    def test_hard_block_does_not_trigger_when_requires_present(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "hard_blocks": [{
                "description": "Block drop without backup",
                "file_pattern": "*drop*",
                "requires_file": "*backup*",
            }],
        })
        # Diff has both "drop" and "backup" files
        apply_skill_review_gates(engine, skill, [
            "migrations/drop_column.sql",
            "backups/backup_001.sql",
        ])

        assert engine.hard_block is False

    def test_hard_block_does_not_trigger_when_pattern_absent(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "hard_blocks": [{
                "description": "Block drop without backup",
                "file_pattern": "*drop*",
                "requires_file": "*backup*",
            }],
        })
        # Diff has no "drop" file at all
        apply_skill_review_gates(engine, skill, ["src/utils.py"])

        assert engine.hard_block is False

    def test_risk_elevator_elevates_to_high(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "risk_elevators": [{
                "description": "Elevate SQL to high",
                "file_pattern": "*.sql",
                "to_level": "high",
            }],
        })
        apply_skill_review_gates(engine, skill, ["query.sql"])

        assert engine.level == RiskLevel.HIGH

    def test_risk_elevator_elevates_to_medium(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "risk_elevators": [{
                "description": "Elevate config to medium",
                "file_pattern": "*.config",
                "to_level": "medium",
            }],
        })
        apply_skill_review_gates(engine, skill, ["app.config"])

        assert engine.level == RiskLevel.MEDIUM

    def test_required_file_missing_adds_signal(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "required_files": ["*migration*"],
        })
        # Diff has no migration file
        apply_skill_review_gates(engine, skill, ["src/utils.py"])

        assert any("required file missing" in c.lower() for c in engine.concerns)

    def test_required_file_present_no_signal(self):
        engine = RiskEngine()
        skill = _make_skill(review_gates={
            "required_files": ["*migration*"],
        })
        apply_skill_review_gates(engine, skill, ["migrations/001_add.py"])

        assert not any("required file missing" in c.lower() for c in engine.concerns)

    def test_no_gates_no_signals(self):
        engine = RiskEngine()
        skill = _make_skill()
        apply_skill_review_gates(engine, skill, ["src/utils.py"])

        assert len(engine.signals) == 0


class TestGetSkillPromptInstructions:
    """get_skill_prompt_instructions — get prompt text for a skill."""

    def test_returns_instructions_when_skill_exists(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "TestSkill", _make_skill(
            "TestSkill",
            prompt_instructions="Always create backups.\nNever delete without migration.",
        ))

        instructions = get_skill_prompt_instructions("TestSkill", skills_dir)
        assert "Always create backups" in instructions
        assert "Never delete without migration" in instructions

    def test_returns_empty_when_skill_not_found(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assert get_skill_prompt_instructions("Nonexistent", skills_dir) == ""

    def test_returns_empty_when_no_skills_dir(self):
        assert get_skill_prompt_instructions("Anything", None) == ""

    def test_returns_empty_when_dir_does_not_exist(self, tmp_path):
        assert get_skill_prompt_instructions("Anything", tmp_path / "nope") == ""


class TestGetActiveSkill:
    """get_active_skill — look up a skill by name."""

    def test_returns_skill_when_found(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "MySkill", _make_skill("MySkill"))

        skill = get_active_skill("MySkill", skills_dir)
        assert skill is not None
        assert skill["name"] == "MySkill"

    def test_returns_none_when_not_found(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assert get_active_skill("Nope", skills_dir) is None

    def test_returns_none_when_no_dir(self):
        assert get_active_skill("Anything", None) is None


# --------------------------------------------------------------------------- #
# 3. Prompt injection
# --------------------------------------------------------------------------- #


class TestPromptInjection:
    """write_prompt includes skill instructions when active."""

    def test_prompt_includes_skill_instructions(self, tmp_path):
        from acp.agents.base import write_prompt
        from acp.config import RepoConfig, RepoSection, SkillsSection

        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "RefactorDB", _make_skill(
            "RefactorDB",
            prompt_instructions="Always create a backup file before schema changes.",
        ))

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            skills=SkillsSection(
                skills_dir=skills_dir,
                active_skill="RefactorDB",
            ),
        )

        prompt_path = write_prompt(
            user_request="Refactor the users table",
            worktree_path=tmp_path / "worktree",
            artifact_dir=tmp_path / "artifacts",
            repo_config=cfg,
        )

        content = prompt_path.read_text()
        assert "RefactorDB" in content
        assert "Always create a backup file" in content

    def test_prompt_without_skill(self, tmp_path):
        from acp.agents.base import write_prompt
        from acp.config import RepoConfig, RepoSection

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )

        prompt_path = write_prompt(
            user_request="Fix the bug",
            worktree_path=tmp_path / "worktree",
            artifact_dir=tmp_path / "artifacts",
            repo_config=cfg,
        )

        content = prompt_path.read_text()
        assert "Skill:" not in content

    def test_prompt_with_skill_but_no_instructions(self, tmp_path):
        """Skill is active but has no prompt_instructions — no skill section."""
        from acp.agents.base import write_prompt
        from acp.config import RepoConfig, RepoSection, SkillsSection

        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "EmptySkill", _make_skill(
            "EmptySkill",
            prompt_instructions="",
        ))

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            skills=SkillsSection(
                skills_dir=skills_dir,
                active_skill="EmptySkill",
            ),
        )

        prompt_path = write_prompt(
            user_request="Fix the bug",
            worktree_path=tmp_path / "worktree",
            artifact_dir=tmp_path / "artifacts",
            repo_config=cfg,
        )

        content = prompt_path.read_text()
        assert "Skill:" not in content


# --------------------------------------------------------------------------- #
# 4. Review gate integration
# --------------------------------------------------------------------------- #


class TestReviewGateIntegration:
    """review_diff applies skill gates during evaluation."""

    def test_skill_hard_block_in_review(self, tmp_path):
        from acp.config import RepoConfig, RepoSection, SkillsSection
        from acp.gitops.diff import DiffCapture
        from acp.review.diff_reviewer import review_diff

        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "DBSkill", _make_skill(
            "DBSkill",
            review_gates={
                "hard_blocks": [{
                    "description": "Block drop without backup",
                    "file_pattern": "*drop*",
                    "requires_file": "*backup*",
                }],
            },
        ))

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            skills=SkillsSection(
                skills_dir=skills_dir,
                active_skill="DBSkill",
            ),
        )

        diff = DiffCapture(
            changed_files=["migrations/drop_column.sql"],
            insertions=10,
            deletions=5,
            binary_files=[],
            patch="",
            stat="1 file changed",
        )

        review = review_diff(
            diff=diff,
            command_results=[],
            repo_config=cfg,
            artifacts_dir=tmp_path / "artifacts",
        )

        assert review.hard_block is True
        assert review.recommendation.value == "reject"

    def test_skill_risk_elevator_in_review(self, tmp_path):
        from acp.config import RepoConfig, RepoSection, SkillsSection
        from acp.gitops.diff import DiffCapture
        from acp.review.diff_reviewer import review_diff

        skills_dir = tmp_path / "skills"
        _write_skill_yaml(skills_dir, "SQLSkill", _make_skill(
            "SQLSkill",
            review_gates={
                "risk_elevators": [{
                    "description": "Elevate SQL to high",
                    "file_pattern": "*.sql",
                    "to_level": "high",
                }],
            },
        ))

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            skills=SkillsSection(
                skills_dir=skills_dir,
                active_skill="SQLSkill",
            ),
        )

        diff = DiffCapture(
            changed_files=["query.sql"],
            insertions=5,
            deletions=0,
            binary_files=[],
            patch="",
            stat="1 file changed",
        )

        review = review_diff(
            diff=diff,
            command_results=[],
            repo_config=cfg,
            artifacts_dir=tmp_path / "artifacts",
        )

        assert review.risk == ModelRiskLevel.HIGH

    def test_no_skill_no_extra_gates(self, tmp_path):
        from acp.config import RepoConfig, RepoSection
        from acp.gitops.diff import DiffCapture
        from acp.review.diff_reviewer import review_diff

        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )

        diff = DiffCapture(
            changed_files=["src/utils.py"],
            insertions=5,
            deletions=0,
            binary_files=[],
            patch="",
            stat="1 file changed",
        )

        review = review_diff(
            diff=diff,
            command_results=[],
            repo_config=cfg,
            artifacts_dir=tmp_path / "artifacts",
        )

        # No skill active, no hard block
        assert review.hard_block is False


# --------------------------------------------------------------------------- #
# 5. Config parsing
# --------------------------------------------------------------------------- #


class TestSkillsConfig:
    """SkillsSection parsing from YAML."""

    def test_default_skills_section(self):
        from acp.config import SkillsSection
        s = SkillsSection()
        assert s.skills_dir is None
        assert s.active_skill == ""

    def test_skills_section_with_values(self, tmp_path):
        from acp.config import SkillsSection
        s = SkillsSection(
            skills_dir=tmp_path / "skills",
            active_skill="RefactorDB",
        )
        assert s.skills_dir is not None
        assert s.active_skill == "RefactorDB"

    def test_repo_config_with_skills(self, tmp_path):
        from acp.config import RepoConfig, RepoSection, SkillsSection
        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
            skills=SkillsSection(
                skills_dir=tmp_path / "skills",
                active_skill="MySkill",
            ),
        )
        assert cfg.skills.active_skill == "MySkill"
        assert cfg.skills.skills_dir is not None

    def test_repo_config_without_skills_defaults(self, tmp_path):
        from acp.config import RepoConfig, RepoSection
        cfg = RepoConfig(
            repo=RepoSection(name="test", path=tmp_path, default_branch="main"),
        )
        assert cfg.skills.active_skill == ""
        assert cfg.skills.skills_dir is None
