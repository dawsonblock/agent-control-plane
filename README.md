# agent-control-plane (ACP) v0.8.0

A Mac-first **local control plane for coding agents**. Its job:

```
Take a coding task
→ isolate it in a git worktree
→ run an agent
→ capture everything
→ run tests
→ review the diff
→ write an evidence report
→ save report into Obsidian
→ promote approved facts into Graphiti memory (later)
→ retrieve useful context for future tasks (later)
```

## The hard rule

```
The event log and evidence reports are truth.
Graphiti is derived memory.
Obsidian is the human review surface.
Haystack is retrieval.
LangGraph is control.
Agents are workers, not decision-makers.
```

## Current scope: v0.7.1 alpha — Engineering execution plan

ACP provides a local evidence loop with hash-chained events, optional Ed25519
signing, artifact manifests, and human approval workflow. The trust layer
binds the complete evidence record — artifacts, task metadata, the
human-facing report, and the evidence policy — to the signed event log.

v0.6.0 introduces **Autonomous Mode** — an opt-in configuration that
bypasses human approval for tasks that pass all gates (tests green, no
secrets, no hard blocks). When enabled, the workflow writes `auto.approved`
and `auto.merged` events to the same hash-chained event log, preserving
the cryptographic evidence trail. The repair loop is enhanced with dynamic
test generation (instructs the agent to write tests when TESTS_MISSING is
flagged) and a circuit breaker (stops the loop when the agent repeats the
same failure).

> **Warning:** Autonomous mode removes the human firewall. Only enable it
> with `docker_sbx` executor (isolated microVM), `block_secret_leaks: true`,
> and `network_policy: locked_down`. Auto-merge (`auto_merge: true`)
> merges the task branch into the default branch without human review.

ACP is under active hardening and should be used for controlled dogfooding,
not production autonomous operation.

This repository currently implements:

| Layer | What | Status |
|-------|------|--------|
| **M0** | Repo scaffold | Stable |
| **M1** | Manual evidence loop — `acp run` produces `final_report.md` + vault note | Stable |
| **M2** | Generic CLI agent adapter | Stable |
| **M3** | LangGraph state machine | Stable (default + only engine) |
| **M4** | Repair loop — bounded retry on test failure | Stable |
| **M5** | Review hardening — risk taxonomy, secret scanner, `GateResult` artifact | Stable |
| **v0.5.x** | Gate consolidation, hash-chained event log, evidence manifest, `acp cleanup`, CI workflow, early-failure evidence | Stable |
| **v0.5.6** | fsync'd event writes, Ed25519 event signing, event timeline in report, SQLite durable event store | Stable |
| **v0.5.7** | Config-driven signing + durable store, `acp verify` + `acp events` CLI commands | Stable |
| **v0.5.8** | Human approval workflow — `acp approve`, `acp reject`, `acp list`, vault note audit trail | Stable |
| **v0.5.9** | Approval-safe evidence: lifecycle events signed + manifest-refreshed; fail-closed signing; task_id validation; `EvidenceLoop` quarantined | Stable |
| **v0.5.10** | Evidence binding: `evidence.finalized` binds artifact content hash to signed event log; composite durable store key; task identity binding; manifest hash recompute; lifecycle manifest; durable mode; `--runs-root`; diff junk filtering; `--debug` on verify | Stable |
| **v0.5.11** | Full evidence binding: `evidence.finalized` binds artifacts + task.json (immutable fields); `evidence.report_bound` binds the human-facing report; missing manifest/report/lifecycle-manifest fails verification; `durable_mode` persisted + fail-closed lifecycle writes; malformed event log suppresses signature success; `acp verify --deep` mode with `DigestCache`; immutable run manifest + separate lifecycle evidence | Stable |
| **v0.5.12** | Lifecycle transaction integrity: full evidence rollback (events.jsonl + final_report.md + lifecycle_manifest.json + SQLite single-transaction); `evidence_config_hash` binds evidence policy to signed event log (prevents silent durable_mode downgrade); `acp verify --check-durable` checks SQLite matches events.jsonl (auto-enabled when durable_mode=required); `DEFAULT_IGNORE_PATTERNS` applied in artifact hashing; task.json.status consistency check; CLI wording corrected | Stable |
| **v0.5.13** | Docker Sandboxes executor backend: `SbxExecutor` runs the coding agent inside an isolated microVM via `sbx run --clone`; clone mode enforced (ACP refuses non-clone); network policy recorded (locked_down/balanced, never open); `sandbox.started`/`sandbox.stopped` events bind executor metadata to signed event log; `capture_diff_from_remote` fetches sandbox remote and diffs agent's private clone; sandbox cleanup (stop/remove) on run completion; `ExecutorSection` in repo config | Stable |
| **v0.5.14** | Pure-projection vault notes: `rerender_vault_note` rebuilds the note from scratch from the event log (no more in-place frontmatter editing or 3-way transactional rollback); TruffleHog integration: `scan_diff` uses TruffleHog for verified secret detection when installed (checks if keys are live before flagging), falls back to regex scanner; `use_trufflehog` config in `ReviewSection`; `review_diff` accepts `worktree_path` for TruffleHog scanning | Stable |
| **v0.5.15** | Sandbox evidence semantics: verifier classifies sandbox events as post-run (fixes critical `acp verify` failure on `sandbox.stopped`); `sandbox.configured` at validation, `sandbox.started` after actual launch, `sandbox.failed` on runtime failure (intention vs fact); network policy strict enum (`locked_down`\|`balanced`, rejects arbitrary strings); `--network` flag passed to `sbx` command; non-main branch support (`cfg.repo.default_branch`); artifact ignore policy consistent (fast and deep verify agree on `__pycache__`/`*.pyc`); persisted `DigestCache` (`digest_cache.json`, deep mode ignores cache) | Stable |
| **v0.5.16** | Executor evidence binding + status semantics: verifier checks `sandbox.configured` event payload for network_policy (not open) and clone_mode (True); `TaskStatus.REJECTED` separated from `TaskStatus.ARCHIVED` (rejection is a first-class human decision, not a cleanup state); `derive_status_from_events` returns "rejected" not "archived"; real sbx E2E smoke test behind `ACP_RUN_REAL_SBX=1` opt-in marker; Docker Sandboxes marked experimental in README | Stable |
| **v0.6.0** | Autonomous mode: `review.autonomous_mode` + `review.auto_merge` config (opt-in, default False); `auto.approved` + `auto.merged` + `test_generation.attempted` events in hash-chained log; `gitops/merge.py` with `merge_to_base` (--no-ff merge, abort on conflict); `auto_approve_node` + `auto_merge_node` in LangGraph; graph routing: `write_report → auto_approve → auto_merge → done` (PASSED + autonomous); enhanced repair loop: `dynamic_test_generation` (TESTS_MISSING → `write_missing_tests_prompt`), `repair_repeat_breaker` circuit breaker (stops loop on repeated failures); `max_repair_attempts` default raised to 5; `auto.approved`/`auto.merged` classified as post-run events in verifier; `derive_status_from_events` treats `auto.approved` as approved; vault note `rerender_vault_note` recognizes `auto.approved` (sets `approved: true`, `memory_status: active`) | Stable |
| **v0.6.1** | M6 Haystack RAG: `rag` optional dependency group (`haystack-ai`, `sentence-transformers`); `HaystackIndexer` with ephemeral `InMemoryDocumentStore` (indexes repo code + vault notes, excludes unapproved task notes); `ContextBuilder` with query pipeline (`SentenceTransformersTextEmbedder` → `InMemoryEmbeddingRetriever`); `build_context_node` writes `artifacts/context_bundle.md` (hash-chained evidence) and links it to the agent prompt; graceful `ImportError` fallback to M1 prompt-only when `rag` extra not installed; `context.built` event records `haystack: true/false`, `retrieved_documents`, `context_bundle_path` | Stable |
| **v0.6.2** | M7 Graphiti temporal memory: `memory` optional dependency group (`graphiti-core[falkordb]`); `docker-compose.yml` for local FalkorDB; `graphiti_client.py` with `ingest_task_to_graphiti` (human firewall: rejects unapproved/archived/ingested notes), `search_graphiti_facts`, `get_temporal_relationships`; `promotion_rules.py` with `should_promote_to_graphiti` (gate), `get_promotion_priority` (4 levels: low/normal/high/urgent), `get_promotion_exclusions`, `get_promotion_metadata`; CLI `acp memory promote` (--dry-run, scans vault/tasks/) + `acp memory search`; auto-promotion in `auto_approve_node` when `memory.promote_reports_by_default=True` (best-effort, silent on ImportError); `memory.promoted` event written to hash-chained log | Stable |
| **v0.6.3** | M8 Skills governance: `SkillsSection` config (`skills_dir`, `active_skill`); `skills/loader.py` with `load_skills` (scans `.yaml` + `SKILL.md` files), `load_skill`, `validate_skill` (schema: name/purpose/rules/hard_blocks/review_gates); `skills/enforcement.py` with `apply_skill_review_gates` (hard_blocks with `requires_file` pattern, risk_elevators with `to_level`, required_files), `get_skill_prompt_instructions`, `get_active_skill`; `write_prompt` injects skill prompt instructions when active; `review_diff` applies skill review gates after default evaluation; example skills: `RefactorDatabase.yaml`, `SecurityReview.yaml` | Stable |
| **v0.6.4** | M9 Agent File registry: `AgentFile` model (name, version, role, command_template, capabilities, sha256, binary_path); `agent_file.py` with `load_agent_file`, `validate_agent_file_data`, `compute_file_hash`, `verify_agent_hash` (SHA-256 of binary, refuses mismatch); `AgentRegistry` (loads `.agent.yaml` from dir, indexes by name, verifies hash); `build_agent` integration: when `agent.agents_dir` is set, looks up agent in registry and verifies hash before execution — hash mismatch raises `AgentConfigError`; example agent files: `claude-code.agent.yaml`, `codex.agent.yaml` | Stable |
| **v0.6.5** | M10 FastAPI control layer: `api` optional dependency group (`fastapi`, `uvicorn[standard]`); `acp/api/server.py` with endpoints: `POST /tasks/run`, `POST /tasks/run/async`, `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/approve`, `POST /tasks/{id}/reject`, `GET /tasks/{id}/events`, `GET /tasks/{id}/report`, `GET /memory/search`, `GET /health`, `POST /config`; `acp serve` CLI command (binds 127.0.0.1, auto-docs at `/docs`); Pydantic request/response models; TestClient-based tests | Stable |
| **v0.6.6** | M11 React UI: Vite + React + TypeScript dashboard in `ui/`; `TaskList` (status badges, click to select), `TaskDetail` (report, event timeline, approve/reject buttons), `RunForm` (submit new tasks), `MemorySearch` (search Graphiti temporal memory); dark theme CSS; `api.ts` typed API client; FastAPI serves built UI at `/dashboard` and `/ui/` (static mount from `ui/dist/`); build with `cd ui && npm run build` | Stable |
| **v0.6.7** | Polish: shared lifecycle service (`acp/evidence/lifecycle.py`) — API approve/reject now uses the same transactional integrity as CLI (signed events, SQLite dual-writes, manifest recompute, rollback); `shell=True` safety in `CLIAgent` — `shlex.split()` by default, shell metacharacters refused in worktree mode unless `agent.allow_shell: true`; SSE streaming (`GET /tasks/stream`) for real-time task updates; UI uses SSE with polling fallback; `DurableTaskStore` orphan recovery on server startup (marks interrupted tasks as FAILED, cleans up worktrees); CORS middleware for dev server; config validation (agent.default, timeout ranges, executor.backend, network_policy); async endpoint returns task_id | Stable |
| **v0.6.8** | Human firewall for autonomous auto-merge: `review.auto_merge_max_risk` config (default `medium`) — `auto_merge_node` refuses to merge when review risk exceeds the ceiling (HIGH-risk database/secret/auth changes always require a human click); event-chain integrity gate — `auto_merge_node` runs `verify_event_chain()` before merging and refuses on a tampered/broken audit trail; new `auto.merge.refused` event (reason: `risk_exceeds_max` or `event_chain_broken`) written to the signed log; `_risk_exceeds` helper (LOW < MEDIUM < HIGH, strictly-above comparison) | Stable |
| **v0.6.9** | Agent federation via MCP + cognitive memory tiers: `federation/` module — `MCPClient` (JSON-RPC 2.0 over stdio, no `mcp` PyPI dependency), `FederationManager` (multi-server discovery, prompt injection, tool-call proxying — agent never touches network); `FederationSection` config (`servers: [{name, command, env, timeout_seconds}]`); federated tools injected into agent prompt via `write_prompt`; `federation.discovered` + `federation.tool_called` events; `memory/tiers.py` — `CognitiveMemoryRetriever` unifying three SAFLA-inspired tiers: Working (Haystack RAG context bundle), Episodic (`EpisodicMemoryStore` cross-run event-log recall), Semantic (Graphiti temporal knowledge graph); `MemoryBundle`/`MemoryItem` data structures with `to_prompt_section()`; graceful degradation when optional extras not installed | Stable |
| **v0.7.0** | M14 Mission layer: `missions/` module — `MissionStore` (directory layout, monotonic `mission_<YYYYMMDD>_<NNNN>` IDs, YAML persistence); `Mission`/`MissionStep`/`MissionStatus` models; `MissionSection` config (`missions_dir`); `acp mission` CLI sub-app (`create`, `list`, `show`, `split`, `complete`); `mission.created` + `mission.completed` events in hash-chained mission event log (`data/missions/<id>/events.jsonl`); mission ID validation (rejects path-shaped IDs); step lifecycle (pending → running → completed/failed); completion gate (all steps must be terminal) | Stable |
| **v0.7.1** | Engineering execution plan: Phase 1 — tech debt (context docstring, `all_passed()` deprecation, silent exception logging, `RiskEngine.recommend()` respects `require_human_approval`); Phase 2 — `DurableTaskStore.check_integrity()` for task.json/SQLite status mismatch detection with `store.integrity_breach` event + fail-closed; `auto.repair_loop_aborted` event for circuit breaker telemetry; `AUTO_MERGE_REFUSED` event includes `risk_factors` list; Phase 3 — `GvisorExecutor` (gVisor/runsc OS-level sandboxing); `custom_secret_regexes` in `ReviewSection` for company-specific token formats (HARD_BLOCK); Phase 4 — React UI: `MissionList`/`MissionDetail`/`Skills` components + `GET /missions` + `GET /skills` API endpoints; Tauri desktop wrapper (`acp serve` sidecar, WebView to `localhost:8000/ui/`); `DurableTaskStore` promoted from Experimental to Stable (61 tests covering save/load/query/rebuild/orphan-recovery/integrity-breach/concurrency) | Current |

All core milestones (M0-M11) are complete.

The non-negotiable rule: **no new layer until the previous layer produces evidence.**

## Quickstart

```bash
cd agent-control-plane
uv sync                    # deps: typer, pydantic, pyyaml, rich, gitpython, langgraph
uv sync --extra dev        # add pytest for local testing
# install all optional extras for full test coverage:
uv sync --extra rag --extra memory --extra dev --extra crypto --extra api
bash scripts/validate.sh   # compileall + ruff + mypy + pytest gate

# run one task against a configured repo
uv run acp run \
  --config configs/repos/example.repo.yaml \
  --task "Fix the failing auth test"
```

Outputs land in `data/runs/<task_id>/` (events, artifacts, report) and `vault/tasks/<task_id>.md`.

## Architecture

```
                 ┌────────────────────┐
                 │      User Task      │
                 └─────────┬──────────┘
                 ┌─────────▼──────────┐
                 │   LangGraph Flow    │
                 └─────────┬──────────┘
        ┌──────────────────┼──────────────────┐
┌───────▼────────┐ ┌───────▼────────┐ ┌───────▼────────┐
│ Worktree Mgr   │ │ Context Builder │ │ Agent Adapter  │
│ git isolation  │ │ (Haystack later)│ │ CLI agents     │
└───────┬────────┘ └───────┬────────┘ └───────┬────────┘
        └──────────────────┼──────────────────┘
                 ┌─────────▼──────────┐
                 │    Test Runner      │
                 ├─────────▼──────────┤
                 │    Diff Reviewer    │
                 ├─────────▼──────────┤
                 │  Evidence Report    │
                 └─────────┬──────────┘
        ┌──────────────────┼──────────────────┐
┌───────▼────────┐ ┌───────▼────────┐ ┌───────▼────────┐
│ Event Store    │ │ Obsidian Vault  │ │ Graphiti Memory│
│ SQLite/JSONL   │ │ Markdown notes  │ │ (later)        │
└────────────────┘ └────────────────┘ └────────────────┘
```

See `docs/` for architecture, roadmap, safety, and memory-model details.
