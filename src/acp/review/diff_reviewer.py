"""Diff reviewer — the M1 risk/recommendation engine.

Looks at the captured diff + command results and produces an advisory
``ReviewResult`` (risk, recommendation, concerns). The recommendation is
advisory: a human always makes the final merge call. But the risk and
concerns are *always visible* in the report and the Obsidian note.

M1 checks (see vault/rules/review-gates.md):
  - changed file count vs review.max_changed_files
  - added line count   vs review.max_added_lines
  - auth / security file paths touched
  - database migration paths touched
  - dependency / lockfile changes
  - behavior changed without test changes

M5 will add secret-string scanning (secret_scanner.py) and a richer risk.py
taxonomy. This module stays focused on the M1 heuristics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from acp.config import RepoConfig
from acp.gitops.diff import DiffCapture
from acp.models import (
    CommandResult,
    Recommendation,
    ReviewResult,
    RiskLevel,
)
from acp.testing.runner import all_passed

# Path heuristics. Lowercase substring matches against posix-style paths.
_AUTH_PATTERNS = (
    "auth", "session", "login", "password", "secret", "token",
    "jwt", "oauth", "saml", "permission", "rbac", "crypto",
)
_DB_PATTERNS = (
    "migration", "migrations/", "schema", "db/", "database/",
    "alembic", "flyway", "prisma/", "schema.prisma",
)
_DEP_PATTERNS = (
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock", "composer.json", "composer.lock",
    "pom.xml", "build.gradle", "build.gradle.kts",
)
_TEST_PATTERNS = (
    "test_", "_test.", ".test.", ".spec.", "/tests/", "/test/",
    "conftest.py", "__tests__/",
)
# Files that count as "behavior" (i.e. not tests/docs/config).
_BEHAVIOR_EXCLUDES = _TEST_PATTERNS + (
    "readme", "changelog", "license", ".md", "docs/",
)


def review_diff(
    *,
    diff: DiffCapture,
    command_results: list[CommandResult],
    repo_config: RepoConfig,
    artifacts_dir: Path,
) -> ReviewResult:
    """Run M1 heuristics over the captured diff; write review.json."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    review = _evaluate(diff, command_results, repo_config)
    (artifacts_dir / "review.json").write_text(
        review.model_dump_json(indent=2)
    )
    return review


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _evaluate(
    diff: DiffCapture,
    command_results: list[CommandResult],
    cfg: RepoConfig,
) -> ReviewResult:
    concerns: list[str] = []
    risk = RiskLevel.LOW
    hard_block = False

    paths_lower = [p.lower() for p in diff.changed_files]

    # --- quantity checks ----------------------------------------------------
    if len(diff.changed_files) > cfg.review.max_changed_files:
        concerns.append(
            f"Changed {len(diff.changed_files)} files; "
            f"max_changed_files={cfg.review.max_changed_files}"
        )
        risk = _raise(risk, RiskLevel.HIGH)
    elif len(diff.changed_files) > cfg.review.max_changed_files // 2:
        risk = _raise(risk, RiskLevel.MEDIUM)

    if diff.insertions > cfg.review.max_added_lines:
        concerns.append(
            f"Added {diff.insertions} lines; "
            f"max_added_lines={cfg.review.max_added_lines}"
        )
        risk = _raise(risk, RiskLevel.HIGH)

    # --- category checks ----------------------------------------------------
    auth_hits = [p for p in paths_lower if any(k in p for k in _AUTH_PATTERNS)]
    if auth_hits and cfg.review.warn_on_auth_changes:
        concerns.append(f"Auth/security-related file(s) changed: {auth_hits}")
        risk = _raise(risk, RiskLevel.MEDIUM)

    db_hits = [p for p in paths_lower if any(k in p for k in _DB_PATTERNS)]
    if db_hits and cfg.review.warn_on_database_changes:
        concerns.append(f"Database migration/schema file(s) changed: {db_hits}")
        risk = _raise(risk, RiskLevel.MEDIUM)

    dep_hits = [p for p in paths_lower if any(k in p for k in _DEP_PATTERNS)]
    if dep_hits:
        concerns.append(f"Dependency/lockfile file(s) changed: {dep_hits}")
        risk = _raise(risk, RiskLevel.MEDIUM)

    # --- tests vs behavior --------------------------------------------------
    behavior_files = [
        p for p in paths_lower
        if not any(k in p for k in _BEHAVIOR_EXCLUDES)
    ]
    test_files = [p for p in paths_lower if any(k in p for k in _TEST_PATTERNS)]
    if behavior_files and not test_files:
        concerns.append(
            "Behavior changed but no test files were modified or added"
        )
        risk = _raise(risk, RiskLevel.MEDIUM)

    # --- command outcomes ---------------------------------------------------
    tests_pass = all_passed(command_results)
    if not tests_pass:
        failed = [r.command for r in command_results if not r.passed and not r.skipped]
        concerns.append(f"Configured command(s) failed: {failed}")
        risk = _raise(risk, RiskLevel.HIGH)

    # --- empty diff ---------------------------------------------------------
    if not diff.changed_files:
        concerns.append("No files changed — agent produced an empty diff")
        risk = _raise(risk, RiskLevel.MEDIUM)

    recommendation = _recommend(risk, tests_pass, hard_block, cfg)
    summary = _summary(risk, recommendation, diff, tests_pass)

    return ReviewResult(
        risk=risk,
        recommendation=recommendation,
        changed_files=diff.changed_files,
        concerns=concerns,
        summary=summary,
        hard_block=hard_block,
    )


def _raise(current: RiskLevel, target: RiskLevel) -> RiskLevel:
    """Monotonic risk increase: only ever raises, never lowers."""
    order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH]
    return target if order.index(target) > order.index(current) else current


def _recommend(
    risk: RiskLevel,
    tests_pass: bool,
    hard_block: bool,
    cfg: RepoConfig,
) -> Recommendation:
    if hard_block:
        return Recommendation.REJECT
    if risk == RiskLevel.HIGH or not tests_pass:
        return Recommendation.REJECT
    if risk == RiskLevel.MEDIUM:
        return Recommendation.REVISE
    return Recommendation.MERGE


def _summary(
    risk: RiskLevel,
    rec: Recommendation,
    diff: DiffCapture,
    tests_pass: bool,
) -> str:
    test_clause = "tests pass" if tests_pass else "tests failing"
    return (
        f"{len(diff.changed_files)} file(s) changed "
        f"({diff.insertions}+, {diff.deletions}-), {test_clause}. "
        f"Risk: {risk.value}. Recommendation: {rec.value}."
    )


# Re-exported for M5's richer taxonomy; harmless now.
evaluate = _evaluate
