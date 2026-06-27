# AGENTS.md — Agent Control Plane

## Development Setup

Install all optional extras for full test coverage:

```bash
uv sync --extra rag --extra memory --extra dev --extra crypto --extra api
```

## Running Tests

Full test suite (all extras installed):

```bash
uv run pytest
```

With real sbx E2E tests (requires Docker Sandboxes installed):

```bash
ACP_RUN_REAL_SBX=1 uv run pytest
```

The 2 remaining skips in `test_context_builder.py` are intentional —
they test the fallback path when rag is NOT installed, so they skip
when rag IS installed (mutually exclusive by design).

## Linting & Type Checking

```bash
uv run ruff check src/acp tests/          # lint (pyflakes + pycodestyle + isort + pyupgrade)
uv run ruff format --check src/acp tests/ # format check
uv run mypy src/acp                        # type check (lenient baseline, non-blocking)
```

## Validation Gate

```bash
bash scripts/validate.sh   # compileall + ruff + ruff format + mypy + pytest --cov
```

## Key Commands

- `acp run` — run a single coding task
- `acp verify` — verify evidence integrity
- `acp approve` — approve a task (human gate)
- `acp reject` — reject a task (human gate)
- `acp list` — list tasks
- `acp events` — show event log
- `acp migrate` — migrate task.json files to SQLite
- `acp memory prune` — prune superseded Graphiti nodes
- `acp memory promote` — promote approved notes to Graphiti
- `acp memory search` — search Graphiti temporal memory
- `acp mission` — manage mission epics
- `acp serve` — start FastAPI HTTP API server
- `acp cleanup` — clean up run artifacts

## Architecture (v0.7.2)

### SQLite Migrations
The durable stores (events + tasks) use a forward-rolling migration engine
(`acp/evidence/migrations.py`) via `PRAGMA user_version`. Migrations are
immutable lists of SQL strings — never modify a published migration, only
append new ones. The JSONL log remains the canonical source of truth;
`rebuild_from_jsonl` is a fallback for catastrophic corruption only.

### Hermetic Agent Isolation
Agent files (`*.agent.yaml`) support an optional `environment` block that
pins the dependency tree via a lockfile hash. When `executor.backend="venv"`,
agents run via `uv run --isolated` in an ephemeral venv, preventing
supply-chain attacks via hijacked Python dependencies. The `agent.started`
event records the locked environment state for cryptographic evidence.

### Persistent RAG
The Haystack indexer supports persistent, incremental indexing. The vector
store is saved per-repo under `data/context_index/` and reused across runs.
Unchanged files (detected via DigestCache) skip re-embedding, making
subsequent `acp run` commands near-instantaneous for unchanged repos. The
`context.built` event includes `rag_stats` with cache hit metrics.
