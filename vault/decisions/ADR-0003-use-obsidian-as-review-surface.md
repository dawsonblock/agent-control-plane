---
type: decision
adr: 0003
title: Use Obsidian as the human review surface
status: active
decided: 2026-06-21
---

# ADR-0003: Use Obsidian as the human review surface

## Context

ACP produces machine artifacts (JSON events, captured stdout, diff patches) that humans must read, judge, and approve before anything becomes memory. Requirements for the review surface:

- Human-readable (markdown, not JSON).
- Supports frontmatter for machine-readable lifecycle fields (`approved`, `memory_status`, `risk`).
- Supports wikilinks (`[[...]]`) so notes cross-reference rules, decisions, failures, and prior tasks.
- Git-friendly so the vault is versioned alongside the code.
- Local-first, no cloud dependency.
- Openable in a tool humans already use for knowledge management.

## Decision

Use **Obsidian** as the review surface, backed by a plain markdown vault at `vault/` in this repo:

```
vault/
  tasks/        ← one note per run, lifecycle frontmatter, the approval gate
  decisions/    ← ADRs
  failures/     ← postmortems
  rules/        ← coding-agent, worktree-safety, review-gates, memory-promotion
  agents/       ← per-agent reference
  repos/        ← per-repo reference
```

Each run copies its `final_report.md` into `vault/tasks/<task_id>.md` with frontmatter:

```yaml
type: task_report
approved: false          # the human flips this
memory_status: draft     # → active → stale/contradicted/archived
graphiti_ingested: false
```

## Consequences

- **+** Humans read markdown in a tool they already know; no new UI to learn for the review step.
- **+** Frontmatter is both human-visible and machine-parseable — Obsidian shows it, ACP reads it.
- **+** Wikilinks let a task report cite the rule it followed (`[[review-gates]]`) or the ADR it instantiated (`[[ADR-0001-use-langgraph]]`).
- **+** The vault is plain files: versionable, greppable, portable. No lock-in.
- **−** Obsidian-the-app is optional; the vault works in any markdown editor. (Accepted — the format is the contract, not the app.)
- **−** Two copies of the report exist (artifact + vault note). The artifact under `data/runs/` is canonical; the vault note is the reviewable projection. The writer never auto-overwrites an approved note.

## Critical rule

> Obsidian is not trusted automatically. Only approved notes can promote memory.

The vault is where the human says "yes." Nothing leaves the vault into Graphiti (M7) without `approved: true` + `memory_status: active`. This is the firewall between "the agent claimed something" and "the system remembers it."

## Alternatives considered

- **Custom web UI first.** Tempting (and coming in M11), but humans shouldn't wait for a UI to start reviewing runs. Markdown is reviewable on day one.
- **JSON-only review.** Unreadable; defeats the purpose of a human gate.
- **Database-backed review surface.** Adds a runtime dependency and hides the review state in a place humans don't naturally look.

## References

- `docs/memory-model.md` — tier 2 (Obsidian) sits between truth (tier 1) and derived memory (tier 3).
- Lifecycle states borrowed from Synthadoc's 5-state machine (`draft|active|contradicted|stale|archived`).
- Reference repos: `Obsidian/` (jsoncanvas, headless, importer, maps).
