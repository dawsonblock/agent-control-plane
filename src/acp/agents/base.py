"""Agent protocol + prompt assembly.

An "agent" in ACP is just a worker that receives a prompt and a worktree,
makes edits inside that worktree, and returns. It never decides what runs
next, never merges, never promotes memory. The control plane (CLI now,
LangGraph in M3) is the only decision-maker.

Every agent — manual shell (M1), custom CLI (M2) — implements ``AgentProtocol``.
The control plane never imports a concrete agent class; it always goes
through ``build_agent`` (see agents/registry.py, added in M2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from acp.config import RepoConfig
from acp.models import AgentResult


def write_prompt(
    *,
    user_request: str,
    worktree_path: Path,
    artifact_dir: Path,
    repo_config: RepoConfig,
    context_bundle_path: Path | None = None,
) -> Path:
    """Write the agent prompt to ``artifacts/agent_prompt.txt`` and return it.

    This is exactly what the agent was told — frozen as evidence. The prompt
    is intentionally minimal in M1 (no Haystack context yet); M6 prepends a
    context_bundle.md when available.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = artifact_dir / "agent_prompt.txt"

    repo = repo_config.repo
    cmds = repo_config.commands
    review = repo_config.review

    context_section = ""
    if context_bundle_path is not None:
        context_section = (
            f"\n\nRelevant context (read this first):\n"
            f"  {context_bundle_path}\n"
        )

    body = f"""You are operating inside an isolated git worktree. A control plane
is watching you. Everything you print is captured. The diff you produce is
reviewed before any human approval.

Worktree:
  {worktree_path}

Repo:
  {repo.name} (default branch: {repo.default_branch})

Task:
  {user_request}{context_section}

Constraints (non-negotiable):
  - Edit only files inside this worktree.
  - Do NOT touch the {repo.default_branch} branch. You have no path to it.
  - Do NOT modify secrets: .env, *.key, *.pem.
  - Keep changes minimal and scoped to the task.
  - Prefer adding/updating tests when you change behavior.
  - Stop and report if the task is ambiguous or impossible.

Runtime commands you may rely on (defined by repo config):
  install    : {cmds.install or "(none)"}
  lint       : {cmds.lint or "(none)"}
  typecheck  : {cmds.typecheck or "(none)"}
  test       : {cmds.test or "(none)"}
  build      : {cmds.build or "(none)"}

Review thresholds that will judge your diff:
  max_changed_files : {review.max_changed_files}
  max_added_lines   : {review.max_added_lines}
  Changes to auth/db/dependency/lockfile files raise risk automatically.

When you are done, simply exit. The control plane captures the diff, runs
the commands, reviews, and writes the report. You do not need to commit.
"""
    prompt_path.write_text(body)
    return prompt_path


@runtime_checkable
class AgentProtocol(Protocol):
    """The contract every agent satisfies.

    ``run`` executes the agent against the prepared prompt + worktree and
    returns a structured result. It must not raise on a nonzero agent exit —
    the control plane still wants to capture the diff and write a report.
    """

    name: str

    def run(
        self,
        *,
        prompt_path: Path,
        worktree_path: Path,
        artifact_dir: Path,
        timeout_seconds: int,
    ) -> AgentResult: ...


def write_repair_prompt(
    *,
    original_request: str,
    worktree_path: Path,
    artifact_dir: Path,
    repo_config: RepoConfig,
    failures: list[dict[str, object]],
    attempt: int,
    max_attempts: int,
    tests_missing: bool = False,
) -> Path:
    """Write a repair prompt to ``artifacts/repair_prompt_<attempt>.txt``.

    Per the M4 spec: "repair prompt must include failed command output." This
    gives the agent the original task, the exact failing commands with their
    captured stdout/stderr, and an explicit instruction to fix the failing
    tests (not delete or skip them). Each attempt is a separate artifact so
    the evidence trail shows exactly what each repair round was told.

    v0.6.0: When ``tests_missing`` is True, delegates to
    ``write_missing_tests_prompt`` which instructs the agent to write new
    tests covering its changes (dynamic test generation).
    """
    if tests_missing:
        return write_missing_tests_prompt(
            original_request=original_request,
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            repo_config=repo_config,
            failures=failures,
            attempt=attempt,
            max_attempts=max_attempts,
        )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = artifact_dir / f"repair_prompt_{attempt}.txt"
    repo = repo_config.repo
    cmds = repo_config.commands

    failure_blocks: list[str] = []
    for i, f in enumerate(failures, start=1):
        failure_blocks.append(
            f"--- failure {i} ---\n"
            f"command: {f['command']}\n"
            f"exit_code: {f['exit_code']}\n"
            f"stdout:\n{f['stdout'] or '(empty)'}\n"
            f"stderr:\n{f['stderr'] or '(empty)'}\n"
        )
    failures_section = "\n".join(failure_blocks) if failure_blocks else "(none captured)"

    body = f"""You are operating inside an isolated git worktree. A previous run
of your task produced failing tests. Repair attempt {attempt} of {max_attempts}.

Worktree:
  {worktree_path}

Repo:
  {repo.name} (default branch: {repo.default_branch})

Original task:
  {original_request}

Failing command output (the signal to fix):
{failures_section}

Instructions:
  - Fix the root cause so the failing commands above pass.
  - Do NOT delete, skip, or weaken tests to make them pass.
  - Do NOT touch the {repo.default_branch} branch or files outside this worktree.
  - Keep the change minimal and scoped to the failure.
  - If the failure cannot be fixed without a larger change, stop and report.

Runtime commands (re-run after your edits by the control plane):
  test       : {cmds.test or '(none)'}
  lint       : {cmds.lint or '(none)'}
  typecheck  : {cmds.typecheck or '(none)'}
"""
    prompt_path.write_text(body)
    return prompt_path


def write_missing_tests_prompt(
    *,
    original_request: str,
    worktree_path: Path,
    artifact_dir: Path,
    repo_config: RepoConfig,
    failures: list[dict[str, object]],
    attempt: int,
    max_attempts: int,
) -> Path:
    """Write a test-generation prompt to ``artifacts/repair_prompt_<attempt>.txt``.

    v0.6.0: When the RiskEngine flags TESTS_MISSING (behavior changed but
    no test files were modified), the repair loop switches to this prompt.
    Instead of fixing failing commands, the agent is instructed to design
    and implement unit tests covering the behavior it already changed.

    The prompt includes the diff of what was changed (via the failures
    context) and strictly instructs: "Behavior was modified but no tests
    were found. Write unit tests for these specific changes."
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = artifact_dir / f"repair_prompt_{attempt}.txt"
    repo = repo_config.repo
    cmds = repo_config.commands

    failure_blocks: list[str] = []
    for i, f in enumerate(failures, start=1):
        failure_blocks.append(
            f"--- failure {i} ---\n"
            f"command: {f['command']}\n"
            f"exit_code: {f['exit_code']}\n"
            f"stdout:\n{f['stdout'] or '(empty)'}\n"
            f"stderr:\n{f['stderr'] or '(empty)'}\n"
        )
    failures_section = "\n".join(failure_blocks) if failure_blocks else "(none captured)"

    body = f"""You are operating inside an isolated git worktree. A previous run
of your task modified behavior but did not include accompanying tests.
Test generation attempt {attempt} of {max_attempts}.

Worktree:
  {worktree_path}

Repo:
  {repo.name} (default branch: {repo.default_branch})

Original task:
  {original_request}

Failing command output (the signal to fix):
{failures_section}

Instructions:
  - Behavior was modified but no tests were found.
  - Write unit tests for these specific changes.
  - The tests must pass when run by the control plane.
  - Do NOT delete, skip, or weaken existing tests.
  - Do NOT touch the {repo.default_branch} branch or files outside this worktree.
  - Keep the tests minimal and scoped to the changed behavior.
  - If the behavior cannot be tested without a larger change, stop and report.

Runtime commands (re-run after your edits by the control plane):
  test       : {cmds.test or '(none)'}
  lint       : {cmds.lint or '(none)'}
  typecheck  : {cmds.typecheck or '(none)'}
"""
    prompt_path.write_text(body)
    return prompt_path
