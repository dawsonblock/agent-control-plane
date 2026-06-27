#!/usr/bin/env bash
# bump_version.sh — atomically update the version across all project files.
#
# Usage:
#   bash scripts/bump_version.sh 0.7.2
#
# Updates:
#   - pyproject.toml          (version = "X.Y.Z")
#   - src/acp/__init__.py     (__version__ = "X.Y.Z")
#   - ui/package.json         ("version": "X.Y.Z")
#   - desktop/src-tauri/tauri.conf.json  ("version": "X.Y.Z")
#   - desktop/src-tauri/Cargo.toml       (version = "X.Y.Z")
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
    echo "Usage: bash scripts/bump_version.sh <X.Y.Z>"
    echo "Example: bash scripts/bump_version.sh 0.7.2"
    exit 1
fi

NEW_VERSION="$1"

# Validate semver format.
if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: version must match X.Y.Z (got: $NEW_VERSION)"
    exit 1
fi

echo "Bumping version to $NEW_VERSION ..."

# pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml && rm -f pyproject.toml.bak
echo "  pyproject.toml: $NEW_VERSION"

# src/acp/__init__.py
sed -i.bak "s/^__version__ = \".*\"/__version__ = \"$NEW_VERSION\"/" src/acp/__init__.py && rm -f src/acp/__init__.py.bak
echo "  src/acp/__init__.py: $NEW_VERSION"

# ui/package.json
if [ -f ui/package.json ]; then
    sed -i.bak "s/\"version\": \".*\"/\"version\": \"$NEW_VERSION\"/" ui/package.json && rm -f ui/package.json.bak
    echo "  ui/package.json: $NEW_VERSION"
fi

# desktop/src-tauri/tauri.conf.json
if [ -f desktop/src-tauri/tauri.conf.json ]; then
    sed -i.bak "s/\"version\": \".*\"/\"version\": \"$NEW_VERSION\"/" desktop/src-tauri/tauri.conf.json && rm -f desktop/src-tauri/tauri.conf.json.bak
    echo "  desktop/src-tauri/tauri.conf.json: $NEW_VERSION"
fi

# desktop/src-tauri/Cargo.toml
if [ -f desktop/src-tauri/Cargo.toml ]; then
    sed -i.bak "s/^version = \".*\"/version = \"$NEW_VERSION\"/" desktop/src-tauri/Cargo.toml && rm -f desktop/src-tauri/Cargo.toml.bak
    echo "  desktop/src-tauri/Cargo.toml: $NEW_VERSION"
fi

echo ""
echo "Done. Verify with: bash scripts/validate.sh"
echo "Then commit: git add -A && git commit -m 'chore: bump version to $NEW_VERSION'"
