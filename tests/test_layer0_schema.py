"""Layer 0 schema tests — forward-declared event types and config sections.

These tests verify that the event types and config sections defined for
upcoming phases (1.1, 1.3, 2.1, 2.2, 3.1, 3.2, 4.1, 4.2) serialize
correctly, round-trip through the event log, and that config validation
works as expected. This unblocks parallel development of those features.
"""

from __future__ import annotations

import pytest

from acp.config import (
    ExecutorSection,
    ProxySection,
    RepoConfig,
    RepoSection,
    RerankingSection,
)
from acp.events import EventWriter, verify_event_chain
from acp.models import EventType

# --------------------------------------------------------------------------- #
# Forward-declared EventType enums
# --------------------------------------------------------------------------- #


# All new event types that were forward-declared in Layer 0.
NEW_EVENT_TYPES = [
    EventType.TASK_STORE_MIGRATED,
    EventType.REVIEW_SECRET_HARD_BLOCK,
    EventType.EXECUTOR_JAILS_CREATED,
    EventType.EXECUTOR_JAILED_RUN_FINISHED,
    EventType.FEDERATION_SERVER_CONNECTED,
    EventType.TASK_SUBTASK_SPAWNED,
    EventType.MEMORY_PRUNED,
]


def test_new_event_types_have_string_values():
    """Every new EventType has a non-empty string value matching its name."""
    for et in NEW_EVENT_TYPES:
        assert isinstance(et.value, str)
        assert et.value  # non-empty


def test_new_event_type_values_are_unique():
    """No two event types share the same string value."""
    all_values = [et.value for et in EventType]
    assert len(all_values) == len(set(all_values)), "duplicate EventType values"


def test_new_event_type_values_are_dotted():
    """Event type values follow the namespace.action convention."""
    for et in NEW_EVENT_TYPES:
        assert "." in et.value, f"{et} value '{et.value}' has no dot"


@pytest.mark.parametrize("event_type", NEW_EVENT_TYPES)
def test_new_event_types_round_trip_through_event_log(tmp_path, event_type):
    """Each new event type can be written to and read from the event log."""
    run_dir = tmp_path / "task_20260626_0001"
    run_dir.mkdir()
    writer = EventWriter("task_20260626_0001", run_dir)
    writer.write(event_type, {"test": "payload"})
    events = writer.read_all()
    assert len(events) == 1
    assert events[0].type == event_type
    assert events[0].payload == {"test": "payload"}


def test_new_event_types_form_valid_hash_chain(tmp_path):
    """Multiple new event types in one log form a valid hash chain."""
    run_dir = tmp_path / "task_20260626_0001"
    run_dir.mkdir()
    writer = EventWriter("task_20260626_0001", run_dir)
    for et in NEW_EVENT_TYPES:
        writer.write(et, {"phase": et.value})
    events = writer.read_all()
    assert len(events) == len(NEW_EVENT_TYPES)
    assert verify_event_chain(events), "hash chain broken with new event types"


# --------------------------------------------------------------------------- #
# ExecutorSection — extended backend values
# --------------------------------------------------------------------------- #


def test_executor_backend_accepts_gvisor():
    """ExecutorSection accepts 'gvisor' as a backend (Phase 2.1)."""
    section = ExecutorSection(backend="gvisor")
    assert section.backend == "gvisor"


def test_executor_backend_accepts_openhands():
    """ExecutorSection accepts 'openhands' as a backend (Phase 2.1)."""
    section = ExecutorSection(backend="openhands")
    assert section.backend == "openhands"


def test_executor_backend_rejects_unknown():
    """ExecutorSection rejects unknown backend values."""
    with pytest.raises(ValueError, match="not valid"):
        ExecutorSection(backend="kubernetes")


def test_executor_backend_defaults_to_worktree():
    """ExecutorSection defaults to 'worktree'."""
    section = ExecutorSection()
    assert section.backend == "worktree"


# --------------------------------------------------------------------------- #
# ProxySection (Phase 2.2)
# --------------------------------------------------------------------------- #


def test_proxy_section_defaults():
    """ProxySection defaults to disabled with port 8080."""
    section = ProxySection()
    assert not section.enabled
    assert section.proxy_port == 8080
    assert section.allowed_domains == []
    assert section.log_artifact == "network_egress.json"


def test_proxy_section_enabled_with_domains():
    """ProxySection accepts enabled state with an allowed domains list."""
    section = ProxySection(
        enabled=True,
        allowed_domains=["pypi.org", "github.com"],
    )
    assert section.enabled
    assert len(section.allowed_domains) == 2


def test_proxy_section_rejects_invalid_port():
    """ProxySection rejects ports outside 1-65535."""
    with pytest.raises(ValueError, match="proxy_port"):
        ProxySection(proxy_port=0)
    with pytest.raises(ValueError, match="proxy_port"):
        ProxySection(proxy_port=70000)


# --------------------------------------------------------------------------- #
# RerankingSection (Phase 4.2)
# --------------------------------------------------------------------------- #


def test_reranking_section_defaults():
    """RerankingSection defaults to disabled with sensible top-k values."""
    section = RerankingSection()
    assert not section.enabled
    assert section.top_k_before_rerank == 20
    assert section.top_k_after_rerank == 5
    assert "cross-encoder" in section.model


def test_reranking_section_enabled():
    """RerankingSection accepts enabled state."""
    section = RerankingSection(enabled=True, top_k_after_rerank=10)
    assert section.enabled
    assert section.top_k_after_rerank == 10


def test_reranking_section_rejects_invalid_k():
    """RerankingSection rejects out-of-range top-k values."""
    with pytest.raises(ValueError, match="top_k_before_rerank"):
        RerankingSection(top_k_before_rerank=0)
    with pytest.raises(ValueError, match="top_k_before_rerank"):
        RerankingSection(top_k_before_rerank=200)
    with pytest.raises(ValueError, match="top_k_after_rerank"):
        RerankingSection(top_k_after_rerank=0)
    with pytest.raises(ValueError, match="top_k_after_rerank"):
        RerankingSection(top_k_after_rerank=100)


# --------------------------------------------------------------------------- #
# RepoConfig integration — new sections are wired in
# --------------------------------------------------------------------------- #


def test_repo_config_has_proxy_section(tmp_path):
    """RepoConfig includes a proxy section with defaults."""
    cfg = RepoConfig(repo=RepoSection(name="demo", path=tmp_path))
    assert hasattr(cfg, "proxy")
    assert isinstance(cfg.proxy, ProxySection)
    assert not cfg.proxy.enabled  # disabled by default


def test_repo_config_has_reranking_section(tmp_path):
    """RepoConfig includes a reranking section with defaults."""
    cfg = RepoConfig(repo=RepoSection(name="demo", path=tmp_path))
    assert hasattr(cfg, "reranking")
    assert isinstance(cfg.reranking, RerankingSection)
    assert not cfg.reranking.enabled  # disabled by default


def test_repo_config_loads_proxy_from_yaml(tmp_path):
    """RepoConfig loads proxy settings from YAML."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = tmp_path / "demo.repo.yaml"
    config_path.write_text(
        f"repo:\n"
        f"  name: demo\n"
        f"  path: {repo_path}\n"
        f"proxy:\n"
        f"  enabled: true\n"
        f"  proxy_port: 9090\n"
        f"  allowed_domains:\n"
        f"    - pypi.org\n"
        f"    - github.com\n"
    )
    from acp.config import load_repo_config

    cfg = load_repo_config(config_path)
    assert cfg.proxy.enabled
    assert cfg.proxy.proxy_port == 9090
    assert "pypi.org" in cfg.proxy.allowed_domains


def test_repo_config_loads_reranking_from_yaml(tmp_path):
    """RepoConfig loads reranking settings from YAML."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = tmp_path / "demo.repo.yaml"
    config_path.write_text(
        f"repo:\n"
        f"  name: demo\n"
        f"  path: {repo_path}\n"
        f"reranking:\n"
        f"  enabled: true\n"
        f"  top_k_after_rerank: 8\n"
    )
    from acp.config import load_repo_config

    cfg = load_repo_config(config_path)
    assert cfg.reranking.enabled
    assert cfg.reranking.top_k_after_rerank == 8


def test_repo_config_loads_gvisor_executor_from_yaml(tmp_path):
    """RepoConfig loads gvisor executor backend from YAML."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config_path = tmp_path / "demo.repo.yaml"
    config_path.write_text(
        f"repo:\n"
        f"  name: demo\n"
        f"  path: {repo_path}\n"
        f"executor:\n"
        f"  backend: gvisor\n"
        f"  network_policy: locked_down\n"
    )
    from acp.config import load_repo_config

    cfg = load_repo_config(config_path)
    assert cfg.executor.backend == "gvisor"
