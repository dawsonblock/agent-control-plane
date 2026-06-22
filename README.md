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

## Current scope: v0.1 (Milestones 0–3)

This repository currently implements the **boring spine**:

| Milestone | What | Gate |
|-----------|------|------|
| **M0** | Repo scaffold: folders, `pyproject`, configs, vault skeleton | `import acp` succeeds |
| **M1** | Manual evidence loop — `acp run` produces `final_report.md` + `vault/tasks/<id>.md` with main untouched | E2E test passes |
| **M2** | Generic CLI agent adapter — swap the coder agent via config only | Agent swap test passes |
| **M3** | Refactor linear CLI into a LangGraph state machine; failed nodes visible, failed tasks still write reports | Graph test passes |

Everything downstream — repair loop (M4), review hardening/secret scan (M5), Haystack retrieval (M6), Graphiti memory (M7), skills governance (M8), Agent File registry (M9), FastAPI (M10), React UI (M11), desktop (M12), OpenHands/Superserve (M13), mission dashboard (M14) — is deliberately deferred until each preceding layer produces evidence.

The non-negotiable rule: **no new layer until the previous layer produces evidence.**

## Quickstart

```bash
cd agent-control-plane
uv venv
uv sync                    # v0.1 deps: typer, pydantic, pyyaml, rich, gitpython
uv sync --extra graph      # add langgraph for M3
uv sync --extra dev        # add pytest

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
