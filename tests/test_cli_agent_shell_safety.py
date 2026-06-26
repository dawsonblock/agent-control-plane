"""Tests for CLIAgent shell safety — shell=True is refused in worktree mode
when the command template contains shell metacharacters."""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.agents.cli_agent import CLIAgent, _needs_shell
from acp.config import (
    AgentSection,
    CommandsSection,
    ExecutorSection,
    RepoConfig,
    RepoSection,
    ReviewSection,
)
from acp.errors import AgentConfigError


def _cfg(
    repo_path: Path,
    command_template: str,
    backend: str = "worktree",
) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(
            default="custom",
            command_template=command_template,
        ),
        commands=CommandsSection(),
        review=ReviewSection(),
        executor=ExecutorSection(backend=backend),
    )


class TestNeedsShell:
    def test_plain_command_no_shell(self):
        assert not _needs_shell("claude-code --prompt-file /tmp/p.txt")

    def test_pipe_requires_shell(self):
        assert _needs_shell("cat prompt.txt | grep foo")

    def test_redirect_requires_shell(self):
        assert _needs_shell("claude-code > output.txt")

    def test_dollar_sign_requires_shell(self):
        assert _needs_shell("echo $HOME")

    def test_semicolon_requires_shell(self):
        assert _needs_shell("echo hi; echo bye")

    def test_backtick_requires_shell(self):
        assert _needs_shell("echo `whoami`")

    def test_newline_requires_shell(self):
        assert _needs_shell("echo hi\necho bye")


class TestWorktreeModeRefusesShell:
    """In worktree mode, shell metacharacters in the template are refused."""

    def test_pipe_refused(self, tmp_path: Path):
        cfg = _cfg(tmp_path, "cat {prompt_path} | grep foo")
        agent = CLIAgent(cfg)
        with pytest.raises(AgentConfigError, match="shell metacharacters"):
            agent.run(
                prompt_path=tmp_path / "prompt.txt",
                worktree_path=tmp_path,
                artifact_dir=tmp_path / "artifacts",
                timeout_seconds=10,
            )

    def test_redirect_refused(self, tmp_path: Path):
        cfg = _cfg(tmp_path, "claude-code > {artifact_dir}/out.txt")
        agent = CLIAgent(cfg)
        with pytest.raises(AgentConfigError, match="shell metacharacters"):
            agent.run(
                prompt_path=tmp_path / "prompt.txt",
                worktree_path=tmp_path,
                artifact_dir=tmp_path / "artifacts",
                timeout_seconds=10,
            )

    def test_plain_command_allowed(self, tmp_path: Path):
        """A command without shell metacharacters should work fine."""
        cfg = _cfg(tmp_path, "echo hello")
        agent = CLIAgent(cfg)
        result = agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert "hello" in (tmp_path / "artifacts" / "agent_stdout.txt").read_text()


class TestDockerSbxAllowsShell:
    """In docker_sbx mode, shell metacharacters are allowed (sandboxed)."""

    def test_pipe_allowed_in_sbx(self, tmp_path: Path):
        cfg = _cfg(tmp_path, "echo hello | cat", backend="docker_sbx")
        agent = CLIAgent(cfg)
        # In docker_sbx mode, shell metacharacters are allowed.
        # The command runs with shell=True — it should succeed.
        result = agent.run(
            prompt_path=tmp_path / "prompt.txt",
            worktree_path=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            timeout_seconds=10,
        )
        # Should NOT raise AgentConfigError — the shell safety check passes.
        assert result.exit_code == 0  # echo hello | cat succeeds
        assert "hello" in (tmp_path / "artifacts" / "agent_stdout.txt").read_text()
