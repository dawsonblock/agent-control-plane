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

## Current scope: v0.5.5 alpha — Dogfood hardening

This repository currently implements:

| Layer | What | Status |
|-------|------|--------|
| **M0** | Repo scaffold | Stable |
| **M1** | Manual evidence loop — `acp run` produces `final_report.md` + vault note | Stable |
| **M2** | Generic CLI agent adapter | Stable |
| **M3** | LangGraph state machine | Stable (default + only engine) |
| **M4** | Repair loop — bounded retry on test failure | Stable |
| **M5** | Review hardening — risk taxonomy, secret scanner, `GateResult` artifact | Stable |
| **v0.5.x** | Gate consolidation, explicit `validation_status`, `--legacy` removed, hash-chained event log, evidence manifest, `acp cleanup`, CI workflow | Current |

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
