"""Shared pytest fixtures.

The disposable-repo fixture is the foundation of every ACP integration test:
it builds a real git repo with an initial commit on ``main``, returns a
``Repo`` object plus the path, and cleans up afterwards. Each test gets a
fresh repo so worktree state never leaks between tests.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

# Every test runs the manual shell agent in its non-interactive mode so the
# evidence loop has a real diff to capture without blocking on a human.
os.environ.setdefault("ACP_TEST", "1")


@dataclass
class DisposableRepo:
    path: Path
    main_head: str  # the sha `main` pointed at before ACP touched anything


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def disposable_repo(tmp_path: Path) -> DisposableRepo:
    """A clean git repo on `main` with one initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@acp.local")
    _git(repo, "config", "user.name", "ACP Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# demo repo\n")
    (repo / "package.json").write_text('{"name": "demo", "scripts": {}}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial commit")
    main_head = _git(repo, "rev-parse", "HEAD").strip()
    return DisposableRepo(path=repo, main_head=main_head)


@pytest.fixture
def isolated_workspace(tmp_path: Path) -> dict[str, Path]:
    """Separate runs-root + vault-root so tests never touch the real ones."""
    return {
        "runs_root": tmp_path / "runs",
        "vault_root": tmp_path / "vault",
    }
