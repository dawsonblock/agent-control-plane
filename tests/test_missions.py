"""M14 Mission layer tests.

Tests the mission feature:

  1. Mission model — serialization, defaults, status transitions
  2. MissionSection config — defaults, validation, absolute path
  3. MissionStore — create, save, load, list, step management, completion
  4. Event emission — mission.created and mission.completed in event log
  5. Event hash chain — mission events form a valid tamper-evident chain
  6. CLI acp mission — create, list, show, split, complete commands
  7. Mission ID validation — rejects path-shaped ids
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp.cli import app
from acp.config import MissionSection, RepoConfig, RepoSection
from acp.events import EventWriter, verify_event_chain
from acp.models import EventType, Mission, MissionStatus, MissionStep
from acp.missions.store import MissionStore, is_valid_mission_id


runner = CliRunner()

# Extracts mission_<YYYYMMDD>_<NNNN> from CLI output.
_MISSION_ID_RE = re.compile(r"mission_\d{8}_\d{4}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_repo_config(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
    )


def _make_mission(
    mission_id: str = "mission_20260626_0001",
    goal: str = "Migrate to React 19",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        goal=goal,
        repo_name="demo",
        repo_path=Path("/tmp/repo"),
        base_branch="main",
    )


# --------------------------------------------------------------------------- #
# 1. Mission model
# --------------------------------------------------------------------------- #


def test_mission_defaults():
    """A new mission has CREATED status and auto-stamped timestamps."""
    m = _make_mission()
    assert m.status == MissionStatus.CREATED
    assert m.created_at == m.updated_at
    assert m.completed_at == ""
    assert m.steps == []
    assert m.description == ""


def test_mission_touch_updates_timestamp():
    """touch() stamps updated_at."""
    m = _make_mission()
    original = m.updated_at
    m.touch()
    assert m.updated_at >= original


def test_mission_step_defaults():
    """A new step is pending with no task_id."""
    step = MissionStep(description="Update package.json")
    assert step.status == "pending"
    assert step.task_id == ""


def test_mission_serialization_roundtrip():
    """Mission model survives JSON round-trip."""
    m = _make_mission()
    m.steps = [MissionStep(description="Step A"), MissionStep(description="Step B")]
    json_str = m.model_dump_json()
    m2 = Mission.model_validate_json(json_str)
    assert m2.mission_id == m.mission_id
    assert m2.goal == m.goal
    assert len(m2.steps) == 2
    assert m2.steps[0].description == "Step A"


# --------------------------------------------------------------------------- #
# 2. MissionSection config
# --------------------------------------------------------------------------- #


def test_mission_section_defaults(tmp_path):
    """MissionSection defaults to data/missions (resolved by MissionStore at use time)."""
    section = MissionSection()
    assert section.missions_dir == Path("data/missions")


def test_mission_section_absolute(tmp_path):
    """missions_dir is resolved to an absolute path."""
    section = MissionSection(missions_dir=tmp_path / "custom_missions")
    assert section.missions_dir.is_absolute()
    assert section.missions_dir.name == "custom_missions"


def test_repo_config_has_mission_section(tmp_path):
    """RepoConfig includes a mission section with defaults."""
    cfg = _make_repo_config(tmp_path)
    assert hasattr(cfg, "mission")
    assert isinstance(cfg.mission, MissionSection)


# --------------------------------------------------------------------------- #
# 3. MissionStore
# --------------------------------------------------------------------------- #


def test_mission_store_create(tmp_path):
    """create() writes mission.yaml and emits mission.created event."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    mission = store.create(
        mission_id=mid,
        goal="Migrate to React 19",
        repo_name="demo",
        repo_path=tmp_path,
        base_branch="main",
        description="Big migration",
    )

    # The returned mission has the right goal.
    assert mission.goal == "Migrate to React 19"
    # mission.yaml exists and is loadable.
    assert store.mission_yaml_path(mid).is_file()
    loaded = store.load(mid)
    assert loaded.goal == "Migrate to React 19"
    assert loaded.description == "Big migration"
    assert loaded.status == MissionStatus.CREATED

    # events.jsonl exists with a mission.created event.
    events_path = store.events_path(mid)
    assert events_path.is_file()
    events = EventWriter(mid, store.mission_dir(mid)).read_all()
    assert len(events) == 1
    assert events[0].type == EventType.MISSION_CREATED
    assert events[0].payload["goal"] == "Migrate to React 19"


def test_mission_store_next_id_monotonic(tmp_path):
    """next_mission_id() produces sequential ids within a day."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    id1 = store.next_mission_id()
    # Create the first mission dir so the sequence advances.
    store.create(
        mission_id=id1,
        goal="Goal 1",
        repo_name="demo",
        repo_path=tmp_path,
    )
    id2 = store.next_mission_id()
    assert id1 != id2
    # Same day prefix, incremented sequence.
    assert id1.rsplit("_", 1)[0] == id2.rsplit("_", 1)[0]
    seq1 = int(id1.rsplit("_", 1)[1])
    seq2 = int(id2.rsplit("_", 1)[1])
    assert seq2 == seq1 + 1


def test_mission_store_add_step(tmp_path):
    """add_step() appends a pending step and persists."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Goal",
        repo_name="demo",
        repo_path=tmp_path,
    )
    mission = store.add_step(mid, "Update dependencies")
    assert len(mission.steps) == 1
    assert mission.steps[0].description == "Update dependencies"
    assert mission.steps[0].status == "pending"

    # Persisted to disk.
    loaded = store.load(mid)
    assert len(loaded.steps) == 1
    assert loaded.steps[0].description == "Update dependencies"


def test_mission_store_mark_step_running(tmp_path):
    """mark_step_running() sets task_id, status, and transitions mission to IN_PROGRESS."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Goal",
        repo_name="demo",
        repo_path=tmp_path,
    )
    store.add_step(mid, "Step A")
    mission = store.mark_step_running(mid, 0, "task_20260626_0001")

    assert mission.steps[0].status == "running"
    assert mission.steps[0].task_id == "task_20260626_0001"
    assert mission.status == MissionStatus.IN_PROGRESS


def test_mission_store_mark_step_completed(tmp_path):
    """mark_step_completed() sets terminal status."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Goal",
        repo_name="demo",
        repo_path=tmp_path,
    )
    store.add_step(mid, "Step A")
    store.mark_step_running(mid, 0, "task_20260626_0001")
    mission = store.mark_step_completed(mid, 0, success=True)
    assert mission.steps[0].status == "completed"


def test_mission_store_complete_success(tmp_path):
    """complete() transitions to COMPLETED and emits mission.completed event."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Goal",
        repo_name="demo",
        repo_path=tmp_path,
    )
    store.add_step(mid, "Step A")
    store.add_step(mid, "Step B")
    store.mark_step_running(mid, 0, "task_20260626_0001")
    store.mark_step_completed(mid, 0, success=True)
    store.mark_step_running(mid, 1, "task_20260626_0002")
    store.mark_step_completed(mid, 1, success=False)

    mission = store.complete(mid)
    assert mission.status == MissionStatus.COMPLETED
    assert mission.completed_at != ""

    # mission.completed event written.
    events = EventWriter(mid, store.mission_dir(mid)).read_all()
    assert len(events) == 2
    assert events[1].type == EventType.MISSION_COMPLETED
    assert events[1].payload["completed_steps"] == 1
    assert events[1].payload["failed_steps"] == 1


def test_mission_store_complete_rejects_non_terminal(tmp_path):
    """complete() refuses if any step is still pending or running."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Goal",
        repo_name="demo",
        repo_path=tmp_path,
    )
    store.add_step(mid, "Step A")
    store.add_step(mid, "Step B")
    store.mark_step_running(mid, 0, "task_20260626_0001")
    store.mark_step_completed(mid, 0, success=True)
    # Step B is still pending.

    with pytest.raises(ValueError, match="non-terminal"):
        store.complete(mid)


def test_mission_store_list(tmp_path):
    """list_missions() returns all missions sorted by id."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    store.create(
        mission_id="mission_20260626_0001",
        goal="Goal A",
        repo_name="demo",
        repo_path=tmp_path,
    )
    store.create(
        mission_id="mission_20260626_0002",
        goal="Goal B",
        repo_name="demo",
        repo_path=tmp_path,
    )
    missions = store.list_missions()
    assert len(missions) == 2
    assert missions[0].mission_id == "mission_20260626_0001"
    assert missions[1].mission_id == "mission_20260626_0002"


def test_mission_store_create_duplicate_raises(tmp_path):
    """create() refuses to overwrite an existing mission dir."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    store.create(
        mission_id="mission_20260626_0001",
        goal="Goal A",
        repo_name="demo",
        repo_path=tmp_path,
    )
    with pytest.raises(FileExistsError):
        store.create(
            mission_id="mission_20260626_0001",
            goal="Goal B",
            repo_name="demo",
            repo_path=tmp_path,
        )


# --------------------------------------------------------------------------- #
# 4 & 5. Event emission + hash chain
# --------------------------------------------------------------------------- #


def test_mission_events_form_valid_hash_chain(tmp_path):
    """mission.created and mission.completed events form a valid hash chain."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Goal",
        repo_name="demo",
        repo_path=tmp_path,
    )
    store.add_step(mid, "Step A")
    store.mark_step_running(mid, 0, "task_20260626_0001")
    store.mark_step_completed(mid, 0, success=True)
    store.complete(mid)

    events = EventWriter(mid, store.mission_dir(mid)).read_all()
    assert len(events) == 2
    assert verify_event_chain(events), "mission event hash chain is broken"


def test_mission_created_event_payload(tmp_path):
    """mission.created event has the expected payload fields."""
    store = MissionStore(missions_dir=tmp_path / "missions")
    mid = store.next_mission_id()
    store.create(
        mission_id=mid,
        goal="Migrate to React 19",
        repo_name="demo",
        repo_path=tmp_path,
        steps=[{"description": "Step A"}, {"description": "Step B"}],
    )

    events = EventWriter(mid, store.mission_dir(mid)).read_all()
    assert events[0].type == EventType.MISSION_CREATED
    assert events[0].payload["mission_id"] == mid
    assert events[0].payload["goal"] == "Migrate to React 19"
    assert events[0].payload["repo_name"] == "demo"
    assert events[0].payload["step_count"] == 2


# --------------------------------------------------------------------------- #
# 6. CLI acp mission
# --------------------------------------------------------------------------- #


def _make_repo_config_file(tmp_path: Path) -> Path:
    """Write a minimal repo.yaml and return its path."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = tmp_path / "demo.repo.yaml"
    config_path.write_text(
        f"repo:\n"
        f"  name: demo\n"
        f"  path: {repo_path}\n"
        f"  default_branch: main\n"
    )
    return config_path


def test_cli_mission_create(tmp_path):
    """`acp mission create` writes mission.yaml + mission.created event."""
    config_path = _make_repo_config_file(tmp_path)
    missions_dir = tmp_path / "missions"

    result = runner.invoke(app, [
        "mission", "create",
        "--config", str(config_path),
        "--goal", "Migrate to React 19",
        "--description", "Big migration",
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "mission" in result.output
    assert "created" in result.output

    # Verify the mission was actually created on disk.
    store = MissionStore(missions_dir=missions_dir)
    missions = store.list_missions()
    assert len(missions) == 1
    assert missions[0].goal == "Migrate to React 19"
    assert missions[0].description == "Big migration"


def test_cli_mission_list(tmp_path):
    """`acp mission list` shows created missions."""
    config_path = _make_repo_config_file(tmp_path)
    missions_dir = tmp_path / "missions"

    # Create two missions.
    for goal in ["Goal A", "Goal B"]:
        r = runner.invoke(app, [
            "mission", "create",
            "--config", str(config_path),
            "--goal", goal,
            "--missions-dir", str(missions_dir),
        ])
        assert r.exit_code == 0, r.output

    result = runner.invoke(app, [
        "mission", "list",
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "Goal A" in result.output
    assert "Goal B" in result.output
    assert "2 total" in result.output


def test_cli_mission_show(tmp_path):
    """`acp mission show` displays mission details and steps."""
    config_path = _make_repo_config_file(tmp_path)
    missions_dir = tmp_path / "missions"

    # Create a mission.
    r = runner.invoke(app, [
        "mission", "create",
        "--config", str(config_path),
        "--goal", "Migrate to React 19",
        "--missions-dir", str(missions_dir),
    ])
    assert r.exit_code == 0, r.output
    # Extract the mission_id from the output.
    match = _MISSION_ID_RE.search(r.output)
    assert match, f"no mission_id in output: {r.output}"
    mid = match.group()  # "mission_20260626_0001"

    # Add a step.
    r = runner.invoke(app, [
        "mission", "split",
        "--mission", mid,
        "--step", "Update package.json",
        "--missions-dir", str(missions_dir),
    ])
    assert r.exit_code == 0, r.output

    # Show the mission.
    result = runner.invoke(app, [
        "mission", "show",
        "--mission", mid,
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "Migrate to React 19" in result.output
    assert "Update package.json" in result.output
    assert "pending" in result.output


def test_cli_mission_split(tmp_path):
    """`acp mission split` adds a step to a mission."""
    config_path = _make_repo_config_file(tmp_path)
    missions_dir = tmp_path / "missions"

    # Create a mission.
    r = runner.invoke(app, [
        "mission", "create",
        "--config", str(config_path),
        "--goal", "Goal",
        "--missions-dir", str(missions_dir),
    ])
    assert r.exit_code == 0, r.output
    match = _MISSION_ID_RE.search(r.output)
    assert match, f"no mission_id in output: {r.output}"
    mid = match.group()

    # Add a step.
    result = runner.invoke(app, [
        "mission", "split",
        "--mission", mid,
        "--step", "Step A",
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "step 1" in result.output
    assert "Step A" in result.output

    # Verify it persisted.
    store = MissionStore(missions_dir=missions_dir)
    mission = store.load(mid)
    assert len(mission.steps) == 1
    assert mission.steps[0].description == "Step A"


def test_cli_mission_complete(tmp_path):
    """`acp mission complete` marks a mission as completed."""
    config_path = _make_repo_config_file(tmp_path)
    missions_dir = tmp_path / "missions"

    # Create a mission with a step.
    r = runner.invoke(app, [
        "mission", "create",
        "--config", str(config_path),
        "--goal", "Goal",
        "--missions-dir", str(missions_dir),
    ])
    assert r.exit_code == 0, r.output
    match = _MISSION_ID_RE.search(r.output)
    assert match, f"no mission_id in output: {r.output}"
    mid = match.group()

    runner.invoke(app, [
        "mission", "split",
        "--mission", mid,
        "--step", "Step A",
        "--missions-dir", str(missions_dir),
    ])

    # Mark the step as completed via the store (CLI for step execution is
    # a future concern — the mission layer tracks step state, not task
    # execution).
    store = MissionStore(missions_dir=missions_dir)
    store.mark_step_running(mid, 0, "task_20260626_0001")
    store.mark_step_completed(mid, 0, success=True)

    # Complete the mission via CLI.
    result = runner.invoke(app, [
        "mission", "complete",
        "--mission", mid,
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output

    # Verify state.
    mission = store.load(mid)
    assert mission.status == MissionStatus.COMPLETED


def test_cli_mission_complete_rejects_non_terminal(tmp_path):
    """`acp mission complete` fails if steps are still pending."""
    config_path = _make_repo_config_file(tmp_path)
    missions_dir = tmp_path / "missions"

    r = runner.invoke(app, [
        "mission", "create",
        "--config", str(config_path),
        "--goal", "Goal",
        "--missions-dir", str(missions_dir),
    ])
    assert r.exit_code == 0, r.output
    match = _MISSION_ID_RE.search(r.output)
    assert match, f"no mission_id in output: {r.output}"
    mid = match.group()

    runner.invoke(app, [
        "mission", "split",
        "--mission", mid,
        "--step", "Step A",
        "--missions-dir", str(missions_dir),
    ])

    # Try to complete without marking the step terminal.
    result = runner.invoke(app, [
        "mission", "complete",
        "--mission", mid,
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 1, result.output
    assert "cannot complete" in result.output


def test_cli_mission_show_invalid_id(tmp_path):
    """`acp mission show` rejects non-canonical mission ids."""
    missions_dir = tmp_path / "missions"
    result = runner.invoke(app, [
        "mission", "show",
        "--mission", "../etc/passwd",
        "--missions-dir", str(missions_dir),
    ])
    assert result.exit_code == 1, result.output
    assert "invalid mission id" in result.output


# --------------------------------------------------------------------------- #
# 7. Mission ID validation
# --------------------------------------------------------------------------- #


def test_is_valid_mission_id():
    """Accepts canonical ids, rejects path-shaped ones."""
    assert is_valid_mission_id("mission_20260626_0001")
    assert is_valid_mission_id("mission_20260101_9999")
    assert not is_valid_mission_id("task_20260626_0001")
    assert not is_valid_mission_id("../etc/passwd")
    assert not is_valid_mission_id("mission_20260626_1")
    assert not is_valid_mission_id("mission_2026062_0001")
    assert not is_valid_mission_id("")
    assert not is_valid_mission_id("mission_20260626_0001/")
