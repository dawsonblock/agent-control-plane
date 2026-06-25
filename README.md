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

## Current scope: v0.5.13 alpha — Docker Sandboxes executor backend

ACP provides a local evidence loop with hash-chained events, optional Ed25519
signing, artifact manifests, and human approval workflow. The trust layer
binds the complete evidence record — artifacts, task metadata, the
human-facing report, and the evidence policy — to the signed event log.
Lifecycle writes (approve/reject) are fully transactional: on failure, all
evidence files are restored to their pre-lifecycle state. v0.5.13 adds
Docker Sandboxes (``sbx``) as an execution backend: the coding agent runs
inside an isolated microVM with its own Docker daemon, filesystem, and
network, and ACP captures the diff from the sandbox's private Git clone
rather than from host worktree mutation. It is under active hardening and
should be used for controlled dogfooding, not production autonomous
operation.

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
| **v0.5.13** | Docker Sandboxes executor backend: `SbxExecutor` runs the coding agent inside an isolated microVM via `sbx run --clone`; clone mode enforced (ACP refuses non-clone); network policy recorded (locked_down/balanced, never open); `sandbox.started`/`sandbox.stopped` events bind executor metadata to signed event log; `capture_diff_from_remote` fetches sandbox remote and diffs agent's private clone; sandbox cleanup (stop/remove) on run completion; `ExecutorSection` in repo config | Current |
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
