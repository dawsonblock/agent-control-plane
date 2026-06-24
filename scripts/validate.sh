#!/usr/bin/env bash
# ACP validation gate — run before every commit/push.
#
# Uses ``uv run`` so the commands always execute inside the project's venv,
# not whatever ``python`` happens to be on PATH. A repo whose whole theme is
# evidence gates cannot ship a validation script that silently uses the wrong
# interpreter.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/2] compileall ==="
uv run python -m compileall -q src tests
echo "PASS"

echo "=== [2/2] pytest ==="
uv run python -m pytest -q
echo "PASS"

echo "=== all validations passed ==="
