"""Repo configuration loader.

Each repo ACP can run against has a `<name>.repo.yaml` describing its
agent, commands, review thresholds, context globs, and memory settings.
This module is the single source of truth for that schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Sub-sections
# --------------------------------------------------------------------------- #


class RepoSection(BaseModel):
    name: str
    path: Path
    default_branch: str = "main"

    @field_validator("path")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v.expanduser().resolve()


class AgentSection(BaseModel):
    default: str = "shell"  # M1: shell. M2 adds: custom
    timeout_seconds: int = 1800
    max_repair_attempts: int = 1
    command_template: str = ""  # used by M2's CLIAgent


class CommandsSection(BaseModel):
    """Empty string => command is skipped at run time."""

    install: str = ""
    lint: str = ""
    typecheck: str = ""
    test: str = ""
    build: str = ""

    def items(self) -> list[tuple[str, str]]:
        """Ordered (name, command) pairs, including empty (skipped) commands."""
        return [
            (name, getattr(self, name))
            for name in ("install", "lint", "typecheck", "test", "build")
        ]


class ReviewSection(BaseModel):
    max_changed_files: int = 20
    max_added_lines: int = 1000
    block_secret_leaks: bool = True
    warn_on_auth_changes: bool = True
    warn_on_database_changes: bool = True
    require_human_approval: bool = True


class ContextSection(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class MemorySection(BaseModel):
    graphiti_group_id: str = ""
    promote_reports_by_default: bool = False


# --------------------------------------------------------------------------- #
# Top-level
# --------------------------------------------------------------------------- #


class RepoConfig(BaseModel):
    """The validated, in-memory form of a `<name>.repo.yaml`."""

    repo: RepoSection
    agent: AgentSection = Field(default_factory=AgentSection)
    commands: CommandsSection = Field(default_factory=CommandsSection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    context: ContextSection = Field(default_factory=ContextSection)
    memory: MemorySection = Field(default_factory=MemorySection)

    # Path the config was loaded from; convenient for messages + events.
    source_path: Path | None = None


def load_repo_config(path: str | Path) -> RepoConfig:
    """Load and validate a repo config YAML.

    Raises FileNotFoundError if missing, ValueError on malformed YAML or
    schema violations (via Pydantic).
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"repo config not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"repo config must be a mapping at top level: {path}")

    cfg = RepoConfig.model_validate(raw)
    cfg.source_path = path
    return cfg
