# Contributing to Agent Control Plane

Thank you for your interest in contributing to ACP. This document covers the
development workflow, code style, and review requirements.

## Development setup

Install all optional extras for full test coverage:

```bash
uv sync --extra rag --extra memory --extra dev --extra crypto --extra api
```

## Running the gate

Before opening a pull request, run the full validation gate:

```bash
bash scripts/validate.sh
```

This runs (in order):

1. `compileall` — syntax check all source and test files
2. `ruff check` — lint
3. `mypy src/acp` — type check
4. `pytest --cov` — tests with coverage

All steps must pass. The gate is also enforced in CI.

### Running tests

```bash
uv run pytest                              # full suite
uv run pytest tests/test_evidence_integrity.py  # single file
uv run pytest -k "approval"                # by keyword
```

With real sbx E2E tests (requires Docker Sandboxes installed):

```bash
ACP_RUN_REAL_SBX=1 uv run pytest
```

The 2 remaining skips in `test_context_builder.py` are intentional — they
test the fallback path when `rag` is NOT installed, so they skip when `rag`
IS installed (mutually exclusive by design).

## Code style

- **Formatter/linter**: ruff (config in `pyproject.toml`). Run
  `uv run ruff check src/acp tests/` and `uv run ruff format --check
  src/acp tests/`.
- **Type checker**: mypy in strict mode. All public functions must have
  type annotations.
- **Line length**: 100 characters.
- **Python**: 3.12+ (target `py312`).
- **Docstrings**: Google style for public functions and classes.
- **Imports**: sorted by ruff/isort; first-party package is `acp`.

## The hard rule

> The event log and evidence reports are truth.
> Graphiti is derived memory.
> Obsidian is the human review surface.
> Haystack is retrieval.
> LangGraph is control.
> Agents are workers, not decision-makers.

Every contribution must respect this invariant: **no new layer until the
previous layer produces evidence.** A feature is "done" only when its gate
passes on a real run, not when its code is written.

## Pull request checklist

- [ ] `bash scripts/validate.sh` passes locally
- [ ] Tests added for new functionality (including error/failure paths)
- [ ] No secrets or credentials committed
- [ ] No `except Exception` without a `# noqa: BLE001` and a comment
      explaining why the error is safe to swallow
- [ ] Public functions have type annotations and docstrings
- [ ] Documentation updated (README, docs/) if the change is user-facing
- [ ] Version bumped in `pyproject.toml` and `src/acp/__init__.py` if
      releasing (use `scripts/bump_version.sh`)

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add gVisor executor backend
fix: resolve hash-chain break on rollback
docs: update roadmap to v0.7.1
test: add gitops/diff parser tests
refactor: split cli.py into submodules
```

## Project structure

```
src/acp/          — source code
  cli.py          — Typer CLI entry point
  graph/          — LangGraph workflow + nodes
  agents/         — agent adapters (CLI, shell, registry)
  executor/       — sandbox executors (sbx, gvisor, openhands)
  evidence/       — event log, durable stores, lifecycle
  review/         — diff reviewer, risk engine, secret scanner
  memory/         — Graphiti client, promotion rules, tiers
  context/        — Haystack RAG context builder
  federation/     — MCP federation client/transport
  missions/       — mission epic store
  api/            — FastAPI server
  gitops/         — worktree, branch, diff, merge helpers
  reports/        — report templates + writer
  vault/          — Obsidian note writer
  skills/         — skills governance loader/enforcement
tests/            — pytest test suite
docs/             — architecture, roadmap, safety, memory-model
scripts/          — validate.sh, bump_version.sh
```

## Reporting bugs

Open a GitHub issue with:

1. ACP version (`uv run acp --version` or check `pyproject.toml`)
2. Python version
3. OS
4. Steps to reproduce
5. Expected vs actual behavior
6. Relevant logs (redact any secrets)
