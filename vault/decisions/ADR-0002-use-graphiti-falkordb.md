---
type: decision
adr: 0002
title: Use Graphiti + FalkorDB for temporal memory
status: active
decided: 2026-06-21
supersedes: []
---

# ADR-0002: Use Graphiti + FalkorDB for temporal memory

## Context

ACP needs a *derived* memory layer: verified facts extracted from approved reports, queryable by future tasks, and able to express that one fact supersedes another over time ("repo X supported Node 20; now it supports Node 22"). Requirements:

- Temporal / bi-temporal edges (`Fact SUPERSEDES Fact`, valid-time intervals).
- Graph model (entities: Repo, Task, File, Failure, Decision...).
- Runs locally on a Mac.
- Ingests only from approved vault notes with citations back to artifacts.

## Decision

Use **Graphiti** (temporal knowledge graph engine) on top of **FalkorDB** (Redis-compatible graph DB), run locally via Docker:

```bash
docker run -p 6379:6379 -p 3000:3000 -it --rm falkordb/falkordb:latest
```

Graphiti is **derived memory only**. It is never the source of truth — the event log and approved vault notes are. Graphiti can be wiped and rebuilt from the vault; the reverse is not true.

## Consequences

- **+** Native temporal edges model "this fact superseded that fact" cleanly.
- **+** Graph queries answer "what runtime applies here?", "what failed before?", "which files are risky?".
- **+** Local Docker deployment fits the Mac-first posture.
- **−** Adds FalkorDB as a runtime dependency (only when memory features are used; M7+).
- **−** Every fact needs a citation or it's not written — ingestion is lossy by design, which is correct but means Graphiti is smaller than the raw vault.

## Scope guard

Graphiti is **deferred to M7**. It is not built until:
1. M1's evidence loop produces trustworthy reports, and
2. M5's Obsidian review surface lets humans approve them.

Per the hard rule: *no new layer until the previous layer produces evidence.* Memory without approved evidence is noise.

## Alternatives considered

- **Plain vector store.** Good for retrieval (Haystack, M6), bad for temporal "fact A supersedes fact B" reasoning.
- **SQLite only.** Workable for events (tier 1), but expressing the supersession graph in SQL is painful.
- **Letta memory.** Letta is optional and not core (per the spec's component table). Graphiti's temporal model fits the "verified facts that age" requirement better.

## References

- `docs/memory-model.md` — tier 3 (Graphiti) is derived from tier 2 (Obsidian).
- Reference repos: `graphiti-main`, `FalkorDB-master`.
