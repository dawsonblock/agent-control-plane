"""Tests for v0.7.0 (Phase 1.1) — SQLite-as-primary task store with feature flag.

Tests the feature-flagged migration from task.json-as-primary to
SQLite-as-primary:

  1. Config validation — task_store_primary field, sqlite requires durable_store
  2. TaskStore dual-write — save() writes to both JSON and SQLite
  3. TaskStore dual-read — load() reads from SQLite when primary="sqlite"
  4. TaskStore fallback — load() falls back to JSON when not in SQLite
  5. CLI acp migrate — imports task.json files, emits task.store_migrated
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp.cli import app
from acp.config import EvidenceSection
from acp.models import Task, TaskStatus
from acp.store import TaskStore


runner = CliRunner()


def _make_repo_config(
    tmp_path: Path,
    *,
    durable_store: Path | None = None,
    task_store_primary: str = "json",
) -> Path:
    """Create a repo.yaml with optional SQLite config."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = tmp_path / "demo.repo.yaml"
    lines = [
        "repo:",
        "  name: demo",
        f"  path: {repo_path}",
    ]
    if durable_store is not None:
        lines.extend([
            "evidence:",
            f"  durable_store: {durable_store}",
            f"  task_store_primary: {task_store_primary}",
        ])
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


# --------------------------------------------------------------------------- #
# 1. Config validation
# --------------------------------------------------------------------------- #


def test_task_store_primary_defaults_to_json():
    """EvidenceSection defaults to task_store_primary='json'."""
    cfg = EvidenceSection()
    assert cfg.task_store_primary == "json"


def test_task_store_primary_accepts_sqlite(tmp_path):
    """EvidenceSection accepts 'sqlite' when durable_store is set."""
    db = tmp_path / "tasks.db"
    cfg = EvidenceSection(durable_store=db, task_store_primary="sqlite")
    assert cfg.task_store_primary == "sqlite"


def test_task_store_primary_sqlite_requires_durable_store():
    """EvidenceSection rejects 'sqlite' without durable_store."""
    with pytest.raises(ValueError, match="requires"):
        EvidenceSection(task_store_primary="sqlite")


def test_task_store_primary_rejects_unknown():
    """EvidenceSection rejects unknown task_store_primary values."""
    with pytest.raises(ValueError, match="not valid"):
        EvidenceSection(task_store_primary="redis")


def test_task_store_primary_in_yaml(tmp_path):
    """Repo config loads task_store_primary from YAML."""
    db = tmp_path / "tasks.db"
    config = _make_repo_config(tmp_path, durable_store=db, task_store_primary="sqlite")
    from acp.config import load_repo_config
    cfg = load_repo_config(config)
    assert cfg.evidence.task_store_primary == "sqlite"
    assert cfg.evidence.durable_store is not None


# --------------------------------------------------------------------------- #
# 2. TaskStore dual-write
# --------------------------------------------------------------------------- #


def test_task_store_dual_writes_to_json_and_sqlite(tmp_path):
    """When durable_store is set, save() writes to both JSON and SQLite."""
    from acp.evidence.durable_task_store import DurableTaskStore

    db = tmp_path / "tasks.db"
    durable = DurableTaskStore(db)
    durable.init()

    store = TaskStore(runs_root=tmp_path / "runs", durable_store=durable)
    store.create(
        task_id="task_20260626_0001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        user_request="test task",
    )

    # JSON file exists.
    assert store.task_json_path("task_20260626_0001").is_file()

    # SQLite has the task.
    loaded = durable.load("task_20260626_0001")
    assert loaded is not None
    assert loaded.task_id == "task_20260626_0001"
    assert loaded.user_request == "test task"

    durable.close()


def test_task_store_save_updates_sqlite(tmp_path):
    """save() after a status change updates both JSON and SQLite."""
    from acp.evidence.durable_task_store import DurableTaskStore

    db = tmp_path / "tasks.db"
    durable = DurableTaskStore(db)
    durable.init()

    store = TaskStore(runs_root=tmp_path / "runs", durable_store=durable)
    task = store.create(
        task_id="task_20260626_0001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        user_request="test task",
    )

    # Change status and save.
    task.status = TaskStatus.PASSED
    store.save(task)

    # SQLite has the updated status.
    loaded = durable.load("task_20260626_0001")
    assert loaded is not None
    assert loaded.status == TaskStatus.PASSED

    durable.close()


# --------------------------------------------------------------------------- #
# 3. TaskStore dual-read (SQLite primary)
# --------------------------------------------------------------------------- #


def test_task_store_load_from_sqlite_when_primary(tmp_path):
    """When primary='sqlite', load() reads from SQLite first."""
    from acp.evidence.durable_task_store import DurableTaskStore

    db = tmp_path / "tasks.db"
    durable = DurableTaskStore(db)
    durable.init()

    store = TaskStore(
        runs_root=tmp_path / "runs",
        durable_store=durable,
        primary="sqlite",
    )
    store.create(
        task_id="task_20260626_0001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        user_request="from sqlite",
    )

    # Load should read from SQLite.
    loaded = store.load("task_20260626_0001")
    assert loaded.user_request == "from sqlite"

    durable.close()


def test_task_store_load_falls_back_to_json(tmp_path):
    """When primary='sqlite' but task not in SQLite, falls back to JSON."""
    from acp.evidence.durable_task_store import DurableTaskStore

    db = tmp_path / "tasks.db"
    durable = DurableTaskStore(db)
    durable.init()

    # Create with JSON-only store (no durable).
    json_store = TaskStore(runs_root=tmp_path / "runs")
    json_store.create(
        task_id="task_20260626_0001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        user_request="json only",
    )

    # Now create a SQLite-primary store pointing at the same runs root.
    sqlite_store = TaskStore(
        runs_root=tmp_path / "runs",
        durable_store=durable,
        primary="sqlite",
    )

    # Load should fall back to JSON since the task isn't in SQLite.
    loaded = sqlite_store.load("task_20260626_0001")
    assert loaded.user_request == "json only"

    durable.close()


def test_task_store_load_from_json_when_primary_json(tmp_path):
    """When primary='json' (default), load() reads from JSON as before."""
    from acp.evidence.durable_task_store import DurableTaskStore

    db = tmp_path / "tasks.db"
    durable = DurableTaskStore(db)
    durable.init()

    store = TaskStore(
        runs_root=tmp_path / "runs",
        durable_store=durable,
        primary="json",
    )
    store.create(
        task_id="task_20260626_0001",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        user_request="json primary",
    )

    loaded = store.load("task_20260626_0001")
    assert loaded.user_request == "json primary"

    durable.close()


# --------------------------------------------------------------------------- #
# 5. CLI acp migrate
# --------------------------------------------------------------------------- #


def test_migrate_dry_run(tmp_path):
    """`acp migrate --dry-run` counts task.json files without importing."""
    db = tmp_path / "tasks.db"
    config = _make_repo_config(tmp_path, durable_store=db)

    # Create some task.json files.
    runs_root = tmp_path / "runs"
    for tid in ["task_20260626_0001", "task_20260626_0002"]:
        task_dir = runs_root / tid
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            Task(
                task_id=tid,
                repo_name="demo",
                repo_path=tmp_path,
                base_branch="main",
                task_branch=f"agent/{tid}",
                worktree_path=task_dir / "worktree",
                user_request="test",
            ).model_dump_json(indent=2)
        )

    result = runner.invoke(app, [
        "migrate",
        "--config", str(config),
        "--runs-root", str(runs_root),
        "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert "found 2 task.json file(s)" in result.output
    assert "dry-run" in result.output


def test_migrate_imports_tasks(tmp_path):
    """`acp migrate` imports task.json files into SQLite."""
    db = tmp_path / "tasks.db"
    config = _make_repo_config(tmp_path, durable_store=db)

    runs_root = tmp_path / "runs"
    for tid in ["task_20260626_0001", "task_20260626_0002"]:
        task_dir = runs_root / tid
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            Task(
                task_id=tid,
                repo_name="demo",
                repo_path=tmp_path,
                base_branch="main",
                task_branch=f"agent/{tid}",
                worktree_path=task_dir / "worktree",
                user_request=f"task {tid}",
            ).model_dump_json(indent=2)
        )

    result = runner.invoke(app, [
        "migrate",
        "--config", str(config),
        "--runs-root", str(runs_root),
    ])
    assert result.exit_code == 0, result.output
    assert "migrated 2 task(s)" in result.output
    assert "task.store_migrated" in result.output

    # Verify the SQLite database has the tasks.
    from acp.evidence.durable_task_store import DurableTaskStore
    store = DurableTaskStore(db)
    store.init()
    assert store.count() == 2
    task = store.load("task_20260626_0001")
    assert task is not None
    assert task.user_request == "task task_20260626_0001"
    store.close()


def test_migrate_no_durable_store_configured(tmp_path):
    """`acp migrate` fails when durable_store is not configured."""
    config = _make_repo_config(tmp_path)  # no durable_store

    result = runner.invoke(app, [
        "migrate",
        "--config", str(config),
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert result.exit_code == 1, result.output
    assert "durable_store is not configured" in result.output


def test_migrate_config_not_found(tmp_path):
    """`acp migrate` exits with error when config file is missing."""
    result = runner.invoke(app, [
        "migrate",
        "--config", str(tmp_path / "nonexistent.yaml"),
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert result.exit_code == 1, result.output
    assert "config file not found" in result.output


def test_migrate_empty_runs_root(tmp_path):
    """`acp migrate` handles an empty runs_root gracefully."""
    db = tmp_path / "tasks.db"
    config = _make_repo_config(tmp_path, durable_store=db)

    runs_root = tmp_path / "runs"
    runs_root.mkdir()

    result = runner.invoke(app, [
        "migrate",
        "--config", str(config),
        "--runs-root", str(runs_root),
    ])
    assert result.exit_code == 0, result.output
    assert "migrated 0 task(s)" in result.output
