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
