# Changelog

All notable changes to agent-control-plane are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — v0.7.2 Architecture Improvements

#### Phase 2: Iterative SQLite Migrations
- `acp/evidence/migrations.py` — lightweight forward-rolling migration engine
  using `PRAGMA user_version` (no SQLAlchemy/Alembic dependency)
- `EVENT_STORE_MIGRATIONS` — immutable migration list for the event store
  (v0→v1: initial schema, v1→v2: signature_algorithm column)
- `TASK_STORE_MIGRATIONS` — immutable migration list for the task store
  (v0→v1: initial schema, v1→v2: orphan_reason column)
- `run_migrations()` — applies pending migrations in `BEGIN EXCLUSIVE`
  transactions with automatic rollback on failure
- `needs_rebuild()` — detects catastrophic corruption (only condition for
  rebuild_from_jsonl fallback)
- DurableEventStore and DurableTaskStore now use the migration engine
  instead of drop-and-rebuild for schema updates

#### Phase 1: Hermetic Agent Isolation
- `EnvironmentSpec` dataclass in agent_file.py — pins agent dependency tree
  via lockfile hash (manager, lockfile, dependencies_hash, python_version)
- `environment` block in agent.yaml schema — optional hermetic isolation spec
- `verify_environment_hash()` — verifies lockfile hash before execution
  (fail-closed on mismatch — prevents supply-chain attacks via hijacked deps)
- `AgentRegistry.verify_environment()` — registry-level environment verification
- `VenvExecutor` — runs Python agents via `uv run --isolated` in ephemeral venvs
  (lighter than docker_sbx, stronger than bare worktree mode)
- `executor.backend="venv"` added to valid backends
- `agent.started` event now records environment context (backend, uv_version,
  python_version, isolated flag) for cryptographic evidence trail

#### Phase 3: Persistent & Incremental RAG
- `HaystackIndexer` persistent mode — JSON-backed document store survives
  across runs, stored per-repo under `data/context_index/`
- Incremental indexing via `DigestCache` — unchanged files (same size + mtime)
  skip re-embedding, making subsequent `acp run` near-instantaneous
- `repo_index_path()` — deterministic per-repo index directory (SHA-256 of path)
- `index_stats` property — telemetry: new_files, cached_files, deleted_files,
  total_files
- `context.built` event now includes `rag_stats` with cache hit metrics
- `ContextBuilder` accepts `persist_path` for persistent RAG index

### Added — Build & CI Improvements
- Ruff linter + formatter configuration (replaces flake8 --select=F)
- Mypy type checker configuration (lenient baseline, non-blocking in gate)
- Coverage measurement via pytest-cov (fail_under=75)
- Expanded CI: lint job, typecheck job, test matrix (Python 3.12 + 3.13, all extras), UI build job, desktop cargo check
- Dependabot config for pip, npm, cargo, and github-actions
- Release workflow (GitHub Releases on tag push)
- LICENSE file (MIT)
- SECURITY.md (vulnerability reporting + security model)
- CONTRIBUTING.md (development workflow + PR checklist)
- Version sync test (asserts pyproject/__init__/desktop/ui versions agree)
- Tests for gitops/diff.py (ignore patterns, stat parsing, real-repo diff capture)
- Tests for gitops/merge.py (merge success, conflict abort, fast-forward check)
- Tests for gitops/worktrees.py + branches.py (create/remove/delete, dirty repo guard)
- Tests for reports/templates.py (report rendering with gates, events, manifest hash)
- Tests for reports/writer.py (write + rerender from run dir)
- Tests for executor/gvisor.py (validation logic, not-installed handling, mocked subprocess)
- Tests for config.py validators (timeout ranges, network policy, executor backend, regex)
- Tests for subtask.py edge cases (cyclic deps, max depth, spawn failure)
- Tests for federation/transport.py error handling (timeout, malformed, connection refused)

### Fixed
- api/server.py: `MissionStore(missions_root=...)` → `MissionStore(missions_dir=...)` (wrong keyword argument)
- evidence/durable_task_store.py: `remove_worktree()` missing required `repo_path` argument
- Import ordering: `from contextlib import asynccontextmanager` moved to top of api/server.py
- printf-style string formatting replaced with .format() in test
- gitops/merge.py: `can_fast_forward` now catches `gitdb.exc.BadName` for missing branches

### Changed
- Version synced to 0.7.1 across pyproject.toml, __init__.py, README, roadmap, ui/package.json
- docs/roadmap.md updated to reflect v0.7.1 as current (was v0.5.8)
- README quickstart updated (removed deprecated `uv venv`, added all-extras install)
- AGENTS.md updated with ruff/mypy commands and complete CLI command list
- docker-compose.yml: pinned falkordb to v4.14.9, added healthcheck
- scripts/validate.sh expanded: compileall + ruff check + ruff format + mypy + pytest --cov
- Codebase reformatted with ruff format (90 files)

## [0.7.1] - 2026-06-26

### Added
- Engineering execution plan: Phase 1 — tech debt fixes
- `DurableTaskStore.check_integrity()` for task.json/SQLite status mismatch detection
- `auto.repair_loop_aborted` event for circuit breaker telemetry
- `GvisorExecutor` (gVisor/runsc OS-level sandboxing)
- `custom_secret_regexes` in ReviewSection for company-specific token formats
- React UI: MissionList/MissionDetail/Skills components + GET /missions + GET /skills API
- Tauri desktop wrapper (acp serve sidecar)
- DurableTaskStore promoted from Experimental to Stable

## [0.7.0] - 2026-06-25

### Added
- M14 Mission layer: MissionStore, Mission/MissionStep/MissionStatus models
- `acp mission` CLI sub-app (create, list, show, split, complete)
- Mission event log with hash-chained events

## [0.6.9] - 2026-06-24

### Added
- Agent federation via MCP (JSON-RPC 2.0 over stdio)
- Cognitive memory tiers (working/episodic/semantic)

## [0.6.8] - 2026-06-24

### Added
- Human firewall for autonomous auto-merge: auto_merge_max_risk config
- Event-chain integrity gate before auto-merge
- auto.merge.refused event

## [0.6.7] - 2026-06-23

### Added
- Shared lifecycle service for API approve/reject
- shell=True safety in CLIAgent
- SSE streaming for real-time task updates
- DurableTaskStore orphan recovery on server startup
- CORS middleware for dev server

## [0.6.6] - 2026-06-22

### Added
- M11 React UI: Vite + React + TypeScript dashboard

## [0.6.5] - 2026-06-21

### Added
- M10 FastAPI control layer with HTTP endpoints
- `acp serve` CLI command

## [0.6.4] - 2026-06-20

### Added
- M9 Agent File registry with SHA-256 hash verification

## [0.6.3] - 2026-06-19

### Added
- M8 Skills governance: YAML playbooks for review/repair/promotion

## [0.6.2] - 2026-06-18

### Added
- M7 Graphiti temporal memory via FalkorDB

## [0.6.1] - 2026-06-17

### Added
- M6 Haystack RAG context retrieval

## [0.6.0] - 2026-06-16

### Added
- Autonomous mode with auto-approve and auto-merge
- Dynamic test generation in repair loop
- Repair circuit breaker

## [0.5.16] - 2026-06-15

### Added
- Executor evidence binding + status semantics
- TaskStatus.REJECTED separated from ARCHIVED

## [0.5.15] - 2026-06-14

### Added
- Sandbox evidence semantics
- Network policy strict enum
- Persisted DigestCache

## [0.5.14] - 2026-06-13

### Added
- Pure-projection vault notes
- TruffleHog integration for verified secret detection

## [0.5.13] - 2026-06-12

### Added
- Docker Sandboxes executor backend (SbxExecutor)

## [0.5.12] - 2026-06-11

### Added
- Lifecycle transaction integrity with full evidence rollback
- evidence_config_hash binding

## [0.5.11] - 2026-06-10

### Added
- Full evidence binding: evidence.finalized + evidence.report_bound
- acp verify --deep mode with DigestCache

## [0.5.10] - 2026-06-09

### Added
- Evidence binding: evidence.finalized binds artifacts + task.json

## [0.5.9] - 2026-06-08

### Added
- Approval-safe evidence: lifecycle events signed + manifest-refreshed

## [0.5.8] - 2026-06-07

### Added
- Human approval workflow: acp approve, acp reject, acp list

## [0.5.7] - 2026-06-06

### Added
- Config-driven signing + durable store
- acp verify + acp events CLI commands

## [0.5.6] - 2026-06-05

### Added
- fsync'd event writes
- Ed25519 event signing
- SQLite durable event store

## [0.5.x] - 2026-06-04

### Added
- Hash-chained event log
- Evidence manifest
- acp cleanup
- CI workflow
- Early-failure evidence

## [0.1.0] - 2026-06-01

### Added
- M0-M5: scaffold, manual evidence loop, CLI agent adapter, LangGraph state machine, repair loop, review hardening
