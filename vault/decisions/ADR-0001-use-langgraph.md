---
type: decision
adr: 0001
title: Use LangGraph for workflow control
status: active
decided: 2026-06-21
---

# ADR-0001: Use LangGraph for workflow control

## Context

ACP's core is a multi-step workflow: create task → check repo → worktree → context → agent → tests → review → report → vault. It needs:

- Explicit nodes with typed state passed between them.
- Conditional transitions (dirty repo → failed; failing test → failed or repair).
- Failure visibility (every transition is an event).
- Checkpointing so runs are inspectable and resumable.

## Decision

Use **LangGraph** as the control layer. Workers (`gitops`, `testing`, `review`, `reports`, `vault`) remain plain Python functions; the graph only orchestrates them and writes events.

## Consequences

- **+** Nodes map 1:1 to spec phases, making the workflow legible.
- **+** Conditional edges express failure paths without deeply nested `if/else`.
- **+** `MemorySaver` gives free checkpointing for run inspection.
- **+** Repair loops (M4) become a subgraph rather than a rewrite.
- **−** Adds a framework dependency (kept in the optional `[graph]` extra through M2; required at M3).
- **−** Team must think in state patches, not linear scripts.

M1 ships a linear CLI first (faster to validate the evidence loop); M3 refactors the *same worker functions* into the graph. The worker code is unchanged by the refactor — only who calls it changes.

## Alternatives considered

- **Plain linear script.** Simpler, but repair loops and failure visibility get messy fast.
- **Temporal / Prefect.** Heavier than a local Mac tool needs; bring external runtime dependencies.
- **Custom state machine.** Reinvents LangGraph poorly.

## References

- `docs/architecture.md` — "LangGraph is control."
- Reference repo: `langgraph-main` (v1.2.6).
