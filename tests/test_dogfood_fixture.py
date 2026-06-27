"""v0.5.5 dogfood fixture — run ACP against a controlled repo, validate invariants.

This is the "real dogfood fixture" from the fix plan: run ACP end-to-end
against a trivial controlled task, then validate:
  - main branch HEAD is unchanged
  - report exists
  - vault note exists
  - event sequence is sane (expected events present, in order)
  - no duplicate terminal events
  - event chain is valid (hash chain)
  - evidence manifest exists and verifies
  - report includes the manifest hash
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from acp.config import AgentSection, CommandsSection, RepoConfig, RepoSection, ReviewSection
from acp.events import EventWriter, verify_event_chain
from acp.evidence.manifest import verify_evidence_manifest
from acp.graph.state import initial_state
from acp.graph.workflow import build_workflow
from acp.models import EventType, TaskStatus
from acp.store import TaskStore


def _config(repo_path: Path) -> RepoConfig:
    return RepoConfig(
        repo=RepoSection(name="demo", path=repo_path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(lint='echo "lint ok"', test='echo "tests passed"'),
        review=ReviewSection(require_human_approval=False),
    )


def _main_head(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


_EXPECTED_EVENTS = [
    EventType.TASK_CREATED,
    EventType.REPO_CHECKED,
    EventType.WORKTREE_CREATED,
    EventType.CONTEXT_BUILT,
    EventType.AGENT_STARTED,
    EventType.AGENT_FINISHED,
    EventType.DIFF_CAPTURED,
    EventType.REVIEW_COMPLETED,
    EventType.REPORT_WRITTEN,
    EventType.VAULT_NOTE_WRITTEN,
    EventType.TASK_COMPLETED,
]


async def test_dogfood_fixture_happy_path(disposable_repo, isolated_workspace):
    """Full ACP run against a controlled repo — all invariants hold."""
    repo = disposable_repo
    runs = isolated_workspace["runs_root"]
    vault = isolated_workspace["vault_root"]

    store = TaskStore(runs_root=runs)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)
    cfg = _config(repo.path)
    state = initial_state(
        config=cfg,
        user_request="Add a hello-world test",
        vault_root=vault,
        runs_root=runs,
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "dogfood"}})

    task_id = result["task_id"]
    run_dir = store.run_dir(task_id)

    # 1. Main branch HEAD unchanged.
    assert _main_head(repo.path) == repo.main_head, "main branch was modified"

    # 2. Report exists.
    report_path = Path(str(result["report_path"]))
    assert report_path.is_file(), "report missing"
    report_body = report_path.read_text()

    # 3. Vault note exists.
    vault_note_path = Path(str(result["vault_note_path"]))
    assert vault_note_path.is_file(), "vault note missing"

    # 4. Event sequence is sane — expected events present and in order.
    events_path = store.events_path(task_id)
    raw_events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    event_types = [e["type"] for e in raw_events]
    for expected in _EXPECTED_EVENTS:
        assert expected.value in event_types, f"missing event: {expected.value}"
    # The expected subset appears in order.
    idx = 0
    for expected in _EXPECTED_EVENTS:
        idx = event_types.index(expected.value, idx)

    # 5. No duplicate terminal events.
    terminal_types = {
        EventType.TASK_COMPLETED.value,
        EventType.TASK_FAILED.value,
        EventType.TASK_NEEDS_REVIEW.value,
    }
    terminal = [e for e in raw_events if e["type"] in terminal_types]
    assert len(terminal) == 1, (
        f"expected exactly 1 terminal event, got {len(terminal)}: {[e['type'] for e in terminal]}"
    )
    assert terminal[0]["type"] == EventType.TASK_COMPLETED.value

    # 6. Event chain is valid (hash chain).
    from acp.models import Event

    event_objs = [
        Event.model_validate_json(l) for l in events_path.read_text().splitlines() if l.strip()
    ]
    assert verify_event_chain(event_objs) is True, "event hash chain is broken"

    # 7. Evidence manifest exists and verifies.
    manifest_path = run_dir / "evidence_manifest.json"
    assert manifest_path.is_file(), "evidence manifest missing"
    assert verify_evidence_manifest(run_dir) is True, "evidence manifest verification failed"

    # 8. Report includes the manifest hash.
    manifest = json.loads(manifest_path.read_text())
    assert manifest["manifest_hash"] in report_body, (
        "report does not contain the evidence manifest hash"
    )

    # 9. Status is PASSED (all gates passed: clean repo, tests pass, diff non-empty, merge rec).
    assert result["status"] == TaskStatus.PASSED


async def test_dogfood_fixture_failing_test(disposable_repo, isolated_workspace):
    """Failing test → FAILED, but all evidence invariants still hold."""
    repo = disposable_repo
    runs = isolated_workspace["runs_root"]
    vault = isolated_workspace["vault_root"]

    cfg = RepoConfig(
        repo=RepoSection(name="demo", path=repo.path, default_branch="main"),
        agent=AgentSection(max_repair_attempts=0),
        commands=CommandsSection(test="exit 1"),
        review=ReviewSection(),
    )

    store = TaskStore(runs_root=runs)
    events = EventWriter("__pending__", store.root / "__pending__")
    wf = build_workflow(store=store, events=events)
    state = initial_state(
        config=cfg,
        user_request="failing task",
        vault_root=vault,
        runs_root=runs,
    )
    result = await wf.ainvoke(state, config={"configurable": {"thread_id": "dogfood-fail"}})

    task_id = result["task_id"]
    run_dir = store.run_dir(task_id)

    # Main unchanged.
    assert _main_head(repo.path) == repo.main_head

    # Report exists.
    assert Path(str(result["report_path"])).is_file()

    # Exactly one terminal event.
    events_path = store.events_path(task_id)
    raw_events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    terminal = [
        e
        for e in raw_events
        if e["type"]
        in {
            EventType.TASK_COMPLETED.value,
            EventType.TASK_FAILED.value,
            EventType.TASK_NEEDS_REVIEW.value,
        }
    ]
    assert len(terminal) == 1
    assert terminal[0]["type"] == EventType.TASK_FAILED.value

    # Event chain valid.
    from acp.models import Event

    event_objs = [
        Event.model_validate_json(l) for l in events_path.read_text().splitlines() if l.strip()
    ]
    assert verify_event_chain(event_objs) is True

    # Manifest exists and verifies.
    assert (run_dir / "evidence_manifest.json").is_file()
    assert verify_evidence_manifest(run_dir) is True
