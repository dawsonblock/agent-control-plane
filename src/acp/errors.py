"""ACP exception types.

Library code (EvidenceLoop, graph nodes, workers) raises these. The Typer
command layer catches them and converts to ``typer.Exit(code=...)`` so the
CLI returns a clean exit code while tests and graph nodes see ordinary
exceptions.
"""

from __future__ import annotations


class ACPError(RuntimeError):
    """Base for all ACP control-flow errors. Carries an exit code for the CLI."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class RepoDirtyError(ACPError):
    """Repo had uncommitted changes at start; nothing was created."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class WorktreeError(ACPError):
    """Worktree/branch creation failed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=3)


class AgentConfigError(ACPError):
    """Agent selection from config was invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=4)
