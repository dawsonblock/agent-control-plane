"""Tests that all version references across the project stay in sync.

The canonical version lives in ``pyproject.toml`` (``[project].version``).
Other locations — ``src/acp/__init__.py``, the Tauri desktop config, the UI
package.json, and the README — must all reference the same version so
releases don't ship mismatched metadata.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _read_pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def _read_init_version() -> str:
    import acp

    return acp.__version__


# --------------------------------------------------------------------------- #
# Core version consistency
# --------------------------------------------------------------------------- #


def test_pyproject_version_matches_init():
    """pyproject.toml version matches src/acp/__init__.py __version__."""
    pyproject_version = _read_pyproject_version()
    init_version = _read_init_version()
    assert pyproject_version == init_version, (
        f"pyproject.toml version ({pyproject_version}) != acp.__version__ ({init_version})"
    )


def test_version_is_valid_semver():
    """The project version matches the X.Y.Z semantic versioning pattern."""
    version = _read_pyproject_version()
    assert _SEMVER_RE.match(version), f"version {version!r} is not a valid semver (X.Y.Z)"


# --------------------------------------------------------------------------- #
# Desktop (Tauri) — skip if absent
# --------------------------------------------------------------------------- #


def test_desktop_version_matches():
    """desktop/src-tauri/tauri.conf.json version matches pyproject version."""
    tauri_conf = REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
    if not tauri_conf.is_file():
        pytest.skip("desktop/src-tauri/tauri.conf.json not found")
    data = json.loads(tauri_conf.read_text())
    assert data["version"] == _read_pyproject_version(), (
        f"tauri.conf.json version ({data['version']}) != "
        f"pyproject version ({_read_pyproject_version()})"
    )


# --------------------------------------------------------------------------- #
# UI package.json — skip if absent
# --------------------------------------------------------------------------- #


def test_ui_version_matches():
    """ui/package.json version matches pyproject version."""
    pkg = REPO_ROOT / "ui" / "package.json"
    if not pkg.is_file():
        pytest.skip("ui/package.json not found")
    data = json.loads(pkg.read_text())
    assert data["version"] == _read_pyproject_version(), (
        f"ui/package.json version ({data['version']}) != "
        f"pyproject version ({_read_pyproject_version()})"
    )


# --------------------------------------------------------------------------- #
# README mentions the current version
# --------------------------------------------------------------------------- #


def test_readme_mentions_version():
    """README.md mentions the current version somewhere."""
    readme = REPO_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("README.md not found")
    version = _read_pyproject_version()
    text = readme.read_text()
    # Accept either bare "0.7.1" or "v0.7.1" mentions.
    assert version in text or f"v{version}" in text, (
        f"README.md does not mention the current version ({version})"
    )
