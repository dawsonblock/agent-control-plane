#!/usr/bin/env bash
# ACP validation gate — run before every commit/push.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/2] compileall ==="
python -m compileall -q src tests
echo "PASS"

echo "=== [2/2] pytest ==="
python -m pytest -q
echo "PASS"

echo "=== all validations passed ==="
