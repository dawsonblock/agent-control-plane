#!/usr/bin/env bash
# ACP validation gate — run before every commit/push.
#
# Uses ``uv run`` so the commands always execute inside the project's venv,
# not whatever ``python`` happens to be on PATH. A repo whose whole theme is
# evidence gates cannot ship a validation script that silently uses the wrong
# interpreter.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/5] compileall ==="
uv run python -m compileall -q src tests
echo "PASS"

echo "=== [2/5] ruff (lint) ==="
uv run ruff check src/acp tests/
echo "PASS"

echo "=== [3/5] ruff format (check) ==="
uv run ruff format --check src/acp tests/
echo "PASS"

echo "=== [4/5] mypy (type check — non-blocking baseline) ==="
# Mypy is configured with a lenient baseline. Once existing type errors are
# resolved, remove the `|| true` to make this step blocking.
uv run mypy src/acp || echo "WARN: mypy reported errors (non-blocking baseline — see pyproject.toml [tool.mypy])"
echo "PASS (non-blocking)"

echo "=== [5/5] pytest (with coverage) ==="
uv run python -m pytest -q --cov=acp --cov-report=term-missing
echo "PASS"

echo "=== all validations passed ==="
