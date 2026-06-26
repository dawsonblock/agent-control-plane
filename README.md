# agent-control-plane (ACP)

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

## Current scope: v0.6.0 alpha — Autonomous mode

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
> and `network_policy: locked_down`. Auto-merge (`auto_merge_on_pass: true`)
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
| **v0.6.0** | Autonomous mode: `review.autonomous_mode` + `review.auto_merge_on_pass` config (opt-in, default False); `auto.approved` + `auto.merged` events in hash-chained log; `gitops/merge.py` with `merge_to_base` (--no-ff merge, abort on conflict); `auto_approve_node` + `auto_merge_node` in LangGraph; graph routing: `write_report → auto_approve → auto_merge → done` (PASSED + autonomous); enhanced repair loop: `dynamic_test_generation` (TESTS_MISSING → agent writes tests), `repair_repeat_breaker` circuit breaker (stops loop on repeated failures); `auto.approved`/`auto.merged` classified as post-run events in verifier; `derive_status_from_events` treats `auto.approved` as approved | Current |
| **Experimental** | `DurableTaskStore` — implemented as library code, not yet integrated into the workflow | Experimental |

Everything downstream — Haystack retrieval (M6), Graphiti memory (M7), skills governance (M8), Agent File registry (M9), FastAPI (M10), React UI (M11) — is deliberately deferred.

The non-negotiable rule: **no new layer until the previous layer produces evidence.**

## Quickstart

```bash
cd agent-control-plane
uv venv
uv sync                    # deps: typer, pydantic, pyyaml, rich, gitpython, langgraph
uv sync --extra dev        # add pytest for local testing
bash scripts/validate.sh   # compileall + pytest gate (uses uv run internally)

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
