# Architecture

## Purpose

`agent-control-plane` (ACP) is a **Mac-first local control plane for coding agents**. It turns a coding task into an auditable, isolated, evidence-backed loop:

```
task → git worktree → agent → tests → diff review → evidence report → Obsidian
```

The system's value is *trust*: every claim an agent makes is grounded in captured stdout/stderr, a captured diff, and a review record. Nothing is asserted without evidence.

## The hard rule

```
The event log and evidence reports are truth.
Graphiti is derived memory.
Obsidian is the human review surface.
Haystack is retrieval.
LangGraph is control.
Agents are workers, not decision-makers.
```

This ordering is load-bearing:

- **Event log + evidence reports are truth.** If it's not in `events.jsonl` or `final_report.md`, it didn't happen. Memory and search are *derived* from these and can be wrong; the source artifacts cannot.
- **Graphiti is derived.** It's a cache of verified facts, never the system of record. If Graphiti and the vault disagree, the vault wins.
- **Obsidian is the human review surface.** Humans read markdown, not JSON. Approval happens here (`approved: true`), and only approved notes can promote into memory.
- **Haystack is retrieval.** It fetches relevant context; it does not decide what's true.
- **LangGraph is control.** The graph decides what runs next; agents do not.
- **Agents are workers.** An agent edits files inside one worktree and returns. It never decides to merge, never edits main, never promotes its own memory.

## Layers (current build: M0–M3)

```
CLI / LangGraph ── control
        │
   ┌────┴────┬────────┬──────────┐
 gitops    agents  testing     review   ── workers (pure functions over artifacts)
   │        │       │            │
 models · config · events · store      ── data + infra (typed, validated)
   │
 reports · vault/obsidian             ── outputs (the evidence)
```

Every worker layer is a set of pure-ish functions that read paths and write artifact files. The control layer (CLI now, LangGraph in M3) orchestrates them and writes an event per transition. This keeps the workers individually testable and lets the control layer be swapped without rewriting logic.

## Isolation model

Every task runs inside a **git worktree** on a dedicated branch off the repo's default branch:

```
<repo>                          ← main, never touched by ACP
  └─ worktree at data/runs/<task_id>/worktree
       on branch agent/<task_id>
```

The acceptance gate for every run: **the main branch HEAD is unchanged**. ACP never commits to or pushes from main. If the repo is dirty when a task starts, ACP fails fast rather than risk mixing agent changes with uncommitted human work.

## Evidence model

A run produces, under `data/runs/<task_id>/`:

| Artifact | Meaning |
|----------|---------|
| `task.json` | The task definition + status |
| `events.jsonl` | Append-only, ordered event log (the source of truth) |
| `worktree/` | The isolated working copy |
| `artifacts/agent_prompt.txt` | Exactly what the agent was told |
| `artifacts/agent_{stdout,stderr}.txt` | What the agent printed |
| `artifacts/commands.json` | Each configured command + exit code + duration |
| `artifacts/{cmd}_{stdout,stderr}.txt` | Per-command captured output |
| `artifacts/diff.patch`, `diff_stat.txt` | The change the agent produced |
| `artifacts/review.json` | Risk + recommendation + concerns |
| `artifacts/final_report.md` | The human-readable report |

The Obsidian note at `vault/tasks/<task_id>.md` is a copy of the report with lifecycle frontmatter. It is the human's surface for review and approval.

## Why these boundaries

- **Pure workers + eventful control** means the same `gitops` / `testing` / `review` functions are reused unchanged when the linear CLI (M1) becomes a LangGraph graph (M3). The refactor changes *who calls them*, not *what they do*.
- **Evidence before memory** means there's always something concrete to promote later. Graphiti (M7) only ingests approved notes that point back at these artifacts.
- **Obsidian as gate** means a human must say yes before anything becomes "memory." This is the single most important safety property: the system cannot gaslight itself.
