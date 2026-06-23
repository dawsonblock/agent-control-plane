"""Diff reviewer — risk/recommendation engine over a captured diff.

Produces an advisory ``ReviewResult`` (risk, recommendation, concerns). The
recommendation is advisory: a human always makes the final merge call. But
the risk, concerns, and any hard-block reason are *always visible* in the
report and the Obsidian note.

M5 taxonomy (see vault/rules/review-gates.md). Checks accumulate typed
``RiskSignal``s via ``RiskEngine``; secret findings are hard blocks (force
REJECT). The engine handles combination and hard-block logic uniformly, so
adding a check is just appending a signal.

Check categories:
  - quantity        — changed file / added line counts vs thresholds
  - secret          — secret-like strings in added lines (HARD BLOCK)
  - env_file        — .env / secrets file changed or added (HARD BLOCK)
  - auth            — auth/security file paths touched
  - database        — migration / schema paths touched
  - dependency      — manifest files changed (package.json, pyproject...)
  - lockfile        — lockfiles changed
  - large_file      — single added file exceeding the size threshold
  - network         — network/client code touched (fetch, http, socket...)
  - permissions     — permission/privilege files touched (chmod, sudoers...)
  - tests_missing   — behavior changed without test changes
  - tests_failing   — configured commands exited nonzero
  - no_validation   — zero non-skipped validation commands ran
  - empty_diff      — agent produced no changes
"""

from __future__ import annotations

from pathlib import Path

from acp.config import RepoConfig
from acp.gitops.diff import DiffCapture
from acp.models import CommandResult, Recommendation, ReviewResult, RiskLevel
from acp.review.risk import RiskCategory, RiskEngine
from acp.review.secret_scanner import scan_patch
from acp.testing.runner import all_passed

# --- path heuristics (lowercase substring matches) ------------------------- #

_AUTH_PATTERNS = (
    "auth", "session", "login", "password", "secret", "token",
    "jwt", "oauth", "saml", "rbac", "crypto",
)
_DB_PATTERNS = (
    "migration", "migrations/", "schema", "db/", "database/",
    "alembic", "flyway", "prisma/", "schema.prisma",
)
_MANIFEST_PATTERNS = (
    "package.json", "requirements.txt", "pyproject.toml",
    "go.mod", "cargo.toml", "gemfile", "composer.json",
    "pom.xml", "build.gradle",
)
_LOCKFILE_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "uv.lock", "go.sum", "cargo.lock",
    "gemfile.lock", "composer.lock", "build.gradle.kts",
)
_NETWORK_PATTERNS = (
    "fetch(", "httpclient", "http.client", "requests", "axios",
    "urllib", "socket", "websocket", "grpc", "rpc/client",
    "net/http", "/client.go",
)
_PERMISSION_PATTERNS = (
    "sudoers", "chmod", "chown", "permissions", "policy.json",
    "iam/", "rbac.yaml", "capability", "acl",
)
# Files whose presence in the diff is itself a hard block (secrets at rest).
_ENV_FILE_PATTERNS = (".env", ".env.", "secrets.yaml", "secrets.yml", "credentials.json")
# Tests/docs don't count as "behavior" for the tests-missing check.
_TEST_PATTERNS = (
    "test_", "_test.", ".test.", ".spec.", "/tests/", "/test/",
    "conftest.py", "__tests__/",
)
_BEHAVIOR_EXCLUDES = _TEST_PATTERNS + ("readme", "changelog", "license", ".md", "docs/")

# A single added file above this many insertions is "large / generated".
_LARGE_FILE_INSERTIONS = 500


def review_diff(
    *,
    diff: DiffCapture,
    command_results: list[CommandResult],
    repo_config: RepoConfig,
    artifacts_dir: Path,
) -> ReviewResult:
    """Run the M5 taxonomy over the captured diff; write review.json."""
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
    engine = RiskEngine()
    paths_lower = [p.lower() for p in diff.changed_files]
    tests_pass = all_passed(command_results)

    # --- secret scanning (HARD BLOCK) ------------------------------------- #
    if cfg.review.block_secret_leaks:
        findings = scan_patch(diff.patch)
        if findings:
            kinds = sorted({f.kind for f in findings})
            engine.add(
                RiskCategory.SECRET,
                f"Secret-like content detected in diff: {kinds}. "
                f"Review and rotate if real.",
                level=RiskLevel.HIGH,
                hard_block=True,
            )

    # --- .env / secrets-file changes (HARD BLOCK) ------------------------- #
    env_hits = [p for p in paths_lower if any(k in p for k in _ENV_FILE_PATTERNS)]
    if env_hits:
        engine.add(
            RiskCategory.ENV_FILE,
            f"Secrets/env file changed: {env_hits}",
            level=RiskLevel.HIGH,
            hard_block=True,
        )

    # --- quantity --------------------------------------------------------- #
    if len(diff.changed_files) > cfg.review.max_changed_files:
        engine.add(
            RiskCategory.QUANTITY,
            f"Changed {len(diff.changed_files)} files; "
            f"max_changed_files={cfg.review.max_changed_files}",
            level=RiskLevel.HIGH,
        )
    elif len(diff.changed_files) > cfg.review.max_changed_files // 2:
        engine.add(
            RiskCategory.QUANTITY,
            f"Changed {len(diff.changed_files)} files "
            f"(>{cfg.review.max_changed_files // 2}, half of threshold)",
            level=RiskLevel.MEDIUM,
        )

    if diff.insertions > cfg.review.max_added_lines:
        engine.add(
            RiskCategory.QUANTITY,
            f"Added {diff.insertions} lines; "
            f"max_added_lines={cfg.review.max_added_lines}",
            level=RiskLevel.HIGH,
        )

    # --- large / generated single file ------------------------------------ #
    large = [p for p in diff.changed_files if _file_insertions(diff.patch, p) >= _LARGE_FILE_INSERTIONS]
    if large:
        engine.add(
            RiskCategory.LARGE_FILE,
            f"Large/generated file(s) (≥{_LARGE_FILE_INSERTIONS} added lines): {large}",
            level=RiskLevel.MEDIUM,
        )

    # --- auth / database / dependency / lockfile -------------------------- #
    if cfg.review.warn_on_auth_changes:
        auth_hits = [p for p in paths_lower if any(k in p for k in _AUTH_PATTERNS)]
        if auth_hits:
            engine.add(
                RiskCategory.AUTH,
                f"Auth/security-related file(s) changed: {auth_hits}",
                level=RiskLevel.MEDIUM,
            )

    if cfg.review.warn_on_database_changes:
        db_hits = [p for p in paths_lower if any(k in p for k in _DB_PATTERNS)]
        if db_hits:
            engine.add(
                RiskCategory.DATABASE,
                f"Database migration/schema file(s) changed: {db_hits}",
                level=RiskLevel.MEDIUM,
            )

    dep_hits = [p for p in paths_lower if any(k in p for k in _MANIFEST_PATTERNS)]
    if dep_hits:
        engine.add(
            RiskCategory.DEPENDENCY,
            f"Dependency manifest file(s) changed: {dep_hits}",
            level=RiskLevel.MEDIUM,
        )

    lock_hits = [p for p in paths_lower if any(k in p for k in _LOCKFILE_PATTERNS)]
    if lock_hits:
        engine.add(
            RiskCategory.LOCKFILE,
            f"Lockfile(s) changed: {lock_hits}",
            level=RiskLevel.MEDIUM,
        )

    # --- network / permissions ------------------------------------------- #
    net_hits = [p for p in paths_lower if any(k in p for k in _NETWORK_PATTERNS)]
    if net_hits:
        engine.add(
            RiskCategory.NETWORK,
            f"Network/client code touched: {net_hits}",
            level=RiskLevel.MEDIUM,
        )

    perm_hits = [p for p in paths_lower if any(k in p for k in _PERMISSION_PATTERNS)]
    if perm_hits:
        engine.add(
            RiskCategory.PERMISSIONS,
            f"Permission/privilege file(s) touched: {perm_hits}",
            level=RiskLevel.HIGH,
        )

    # --- tests vs behavior ------------------------------------------------ #
    behavior_files = [p for p in paths_lower if not any(k in p for k in _BEHAVIOR_EXCLUDES)]
    test_files = [p for p in paths_lower if any(k in p for k in _TEST_PATTERNS)]
    if behavior_files and not test_files:
        engine.add(
            RiskCategory.TESTS_MISSING,
            "Behavior changed but no test files were modified or added",
            level=RiskLevel.MEDIUM,
        )

    # --- command outcomes ------------------------------------------------- #
    non_skipped = [r for r in command_results if not r.skipped]
    if len(non_skipped) == 0:
        engine.add(
            RiskCategory.NO_VALIDATION,
            "No validation commands ran — risk cannot be assessed from test results",
            level=RiskLevel.MEDIUM,
        )
    elif not tests_pass:
        failed = [r.command for r in command_results if not r.passed and not r.skipped]
        engine.add(
            RiskCategory.TESTS_FAILING,
            f"Configured command(s) failed: {failed}",
            level=RiskLevel.HIGH,
        )

    # --- empty diff ------------------------------------------------------- #
    if not diff.changed_files:
        engine.add(
            RiskCategory.EMPTY_DIFF,
            "No files changed — agent produced an empty diff",
            level=RiskLevel.MEDIUM,
        )

    risk = engine.level
    recommendation = engine.recommend(
        tests_pass=tests_pass,
        require_human_approval=cfg.review.require_human_approval,
    )
    return ReviewResult(
        risk=risk,
        recommendation=recommendation,
        changed_files=diff.changed_files,
        concerns=engine.concerns,
        summary=_summary(risk, recommendation, diff, tests_pass, engine),
        hard_block=engine.hard_block,
    )


def _file_insertions(patch: str, path: str) -> int:
    """Count added lines attributable to one file in a unified diff."""
    target = f"b/{path}"
    count = 0
    in_file = False
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            in_file = target in line
        elif in_file and line.startswith("+") and not line.startswith("+++"):
            count += 1
    return count


def _summary(
    risk: RiskLevel,
    rec: Recommendation,
    diff: DiffCapture,
    tests_pass: bool,
    engine: RiskEngine,
) -> str:
    test_clause = "tests pass" if tests_pass else "tests failing"
    block_clause = " [HARD BLOCK]" if engine.hard_block else ""
    return (
        f"{len(diff.changed_files)} file(s) changed "
        f"({diff.insertions}+, {diff.deletions}-), {test_clause}. "
        f"Risk: {risk.value}. Recommendation: {rec.value}.{block_clause}"
    )


# Back-compat re-export (older code imported `evaluate`).
evaluate = _evaluate
