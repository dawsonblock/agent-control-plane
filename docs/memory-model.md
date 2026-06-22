# Memory model

ACP has three tiers of "memory," strictly ordered by trust:

```
1. Event log + evidence reports   ── truth          (data/runs/<task_id>/)
2. Obsidian vault                 ── review surface (vault/)
3. Graphiti temporal graph (M7)   ── derived memory (FalkorDB)
```

Each tier is derived from the one above it. Disagreements resolve downward: the event log beats the vault beats Graphiti.

## Tier 1 — Event log + evidence reports (truth)

Every meaningful action appends a typed event to `data/runs/<task_id>/events.jsonl`:

```
task.created · repo.checked · worktree.created · context.built
agent.started · agent.finished · command.started · command.finished
diff.captured · review.completed · report.written · vault.note_written
human.approved · memory.promoted · task.failed · task.completed
```

The report (`final_report.md`) is the human-readable projection of these events plus the captured artifacts (diff, command outputs, review). **If it's not here, it didn't happen.**

## Tier 2 — Obsidian vault (human review surface)

Each run copies its report into `vault/tasks/<task_id>.md` with frontmatter:

```yaml
type: task_report
task_id: task_20260621_0001
repo: my-app
status: passed
risk: medium
approved: false              # human must flip this
memory_status: draft         # see lifecycle below
graphiti_ingested: false
created: 2026-06-21
sources: [diff.patch, test_stdout.txt, review.json]
```

The vault also holds hand-authored, permanent notes:

- `vault/rules/` — coding-agent rules, memory-promotion rules, review gates, worktree safety
- `vault/decisions/` — architecture decision records (ADRs)
- `vault/failures/` — postmortems (populated as failures occur)
- `vault/repos/`, `vault/agents/` — per-repo and per-agent reference notes

### Lifecycle states (borrowed from Synthadoc)

Every note moves through a 5-state machine:

```
draft  →  active  →  stale  →  archived
                ↘  contradicted  ↗
```

| State | Meaning |
|-------|---------|
| `draft` | Just written; not yet trusted |
| `active` | Reviewed, current, trustworthy |
| `stale` | Source changed on disk since the note was written |
| `contradicted` | A newer note supersedes this one (both kept; the newer wins) |
| `archived` | No longer relevant; retained for history |

M1 writes everything as `draft`. Promotion to `active` is a human action (later, a lint pass can propose it). A `contradicted` note is never deleted — the contradiction is part of the record, and Graphiti's `Fact SUPERSEDES Fact` edge is how temporal reasoning works.

## Tier 3 — Graphiti temporal graph (M7, deferred)

Built **only** from notes where:

```yaml
approved: true
memory_status: active
graphiti_ingested: false
```

Entities: `Repo · Task · Agent · File · Command · Test · Failure · Fix · Decision · Constraint · Dependency · Risk · Skill`

Relationships (examples):

```
Task MODIFIED File
Command PRODUCED Failure
Fix RESOLVED Failure
Decision APPROVED Task
Constraint APPLIES_TO Repo
Fact SUPERSEDES Fact        ← the temporal edge, sourced from contradicted notes
```

Graphiti answers questions like *what runtime applies to this repo?*, *what has failed before here?*, *which files are risky?* — always pointing back to the artifact that established the fact. If Graphiti can't cite a source, the fact doesn't exist.

## Critical rule

> Obsidian is not trusted automatically. Only approved notes can promote memory.

This is the firewall between "the agent said something" and "the system remembers it." The agent's report is `draft`. A human reads it, approves it, and *then* it may become memory. The system cannot short-circuit this.
