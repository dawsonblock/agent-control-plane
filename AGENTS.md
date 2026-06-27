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
uv run mypy src/acp                        # type check (strict mode)
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

## Architecture (v0.9.0)

### SQLite Migrations
The durable stores (events + tasks) use a forward-rolling migration engine
(`acp/evidence/migrations.py`) via a per-store `schema_versions` table
(not `PRAGMA user_version`, which is a single integer per database and
can't track per-store versions independently). Migrations are immutable
lists of SQL strings — never modify a published migration, only append
new ones. The JSONL log remains the canonical source of truth;
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

### Mid-Stream Sentinel (v0.7.3)
When `streaming.enabled` is set in the repo config, the CLIAgent switches
from blocking `subprocess.run` to an async streaming path. Each line of
agent stdout is fed to a `StreamSentinel` (`acp/streaming/midstream.py`)
that runs real-time safety checks *before* the agent finishes:

1. **Kill-switch (secret detection)**: Known credential patterns (AWS keys,
   GitHub PATs, private key blocks, JWTs, etc.) in the output stream trigger
   an immediate process kill. Uses a stream-specific scanner
   (`acp/streaming/secret_stream_scanner.py`) that reuses the provider regex
   patterns from `acp.review.secret_scanner` but operates on raw text (no
   `+`-prefix diff-line requirement).

2. **Strange-loop detection**: Near-duplicate output cycles are detected via
   token 3-gram Jaccard similarity against a rolling window. Catches
   attractor/hallucination loops where the agent repeats near-identical output
   with small token drift — not just exact repeats.

3. **Dangerous-path detection**: Configurable regex patterns
   (`streaming.dangerous_path_patterns`) trigger an immediate kill.

When the sentinel kills the agent, a `stream.aborted` event is written to
the hash-chained event log (serialized via `asyncio.Lock` to preserve the
chain invariant). The graph short-circuits to `failed` — tests and review
are skipped on the partial diff from a killed agent. The `AgentResult`
carries `aborted_by_sentinel=True` and `sentinel_abort_reason` for the
`agent.finished` event payload.

Default: disabled (`streaming.enabled: false`). Enable per-repo when using
the `custom` agent (CLIAgent). Has no effect on `shell` or `docker_sbx`.

### SQLite-Primary Event Writes (v0.9.0)
When `evidence.durable_mode="required"`, regular run events (not just
lifecycle events) use SQLite-first write ordering: the event is built,
written to the SQLite durable store (authoritative), then appended to
`events.jsonl` (best-effort mirror). If the SQLite write fails, the event
is nowhere and the run fails cleanly — no orphan JSONL events. If the
JSONL append fails after SQLite succeeds, the event is durable in SQLite
and the JSONL can be rebuilt on startup via
`DurableEventStore.rebuild_jsonl_from_store()`.

### macOS Seatbelt Executor (v0.9.0)
`executor.backend="seatbelt"` runs the agent inside a macOS `sandbox-exec`
kernel sandbox — lightweight OS-level filesystem and network restrictions
without a container runtime. The sandbox profile is generated dynamically
based on the worktree path and `network_policy`, or a custom profile can
be provided via `executor.seatbelt_profile_path`. macOS-only.

### Mission-Aware Memory (v0.9.0)
Mission context (goal, description, step index) is persisted on the
durable `Task` model and threaded into Graphiti episode text, making
memory extraction mission-aware. On `mission.completed`, a best-effort
mission rollup is performed (gated by `promote_reports_by_default`).
