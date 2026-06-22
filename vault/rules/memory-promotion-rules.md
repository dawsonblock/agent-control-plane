---
type: rule
name: memory-promotion-rules
status: active
applies_to: [memory, all-repos]
created: 2026-06-21
---

# Memory promotion rules

How an approved Obsidian note becomes derived memory (Graphiti, M7).

## The firewall

> A note with `approved: false` can never become memory.

This is the single most important safety property. The agent writes reports as `draft`; a human reads them and flips `approved: true`; *only then* may Graphiti ingest them. The system cannot gaslight itself.

## Ingestion criteria (all must hold)

A vault note is eligible for Graphiti ingestion only when:

```yaml
approved: true
memory_status: active          # not draft/stale/contradicted/archived
graphiti_ingested: false       # not already imported
type: task_report              # or decision/rule/failure
```

## Lifecycle gating

The note's `memory_status` participates in the Synthadoc-style state machine:

```
draft → active → stale → archived
            ↘ contradicted ↗
```

- Only `active` notes ingest.
- A later note that `contradicts` an active one flips it to `contradicted` and writes a `Fact SUPERSEDES Fact` edge in Graphiti. The old fact stays queryable but marked superseded.
- A note whose backing artifact changed on disk goes `stale` and won't re-ingest until re-approved.

## What gets extracted

From an eligible task report, Graphiti extracts entities and relationships:

```
Task MODIFIED File
Task RAN Command
Command PRODUCED Failure
Fix RESOLVED Failure
Decision APPROVED Task
Constraint APPLIES_TO Repo
Dependency VERSION_CHANGED_IN Task
Agent EXECUTED Task
Review FLAGGED Risk
Fact SUPERSEDES Fact
```

Every extracted fact carries a citation back to its source artifact (`diff.patch`, `review.json`, etc.). A fact without a citation is not written.

## Never

- Ingest a note that is not `approved: true`.
- Ingest a note whose `memory_status` is not `active`.
- Re-ingest a note already marked `graphiti_ingested: true` without a reason (re-ingestion is for corrections only).
- Allow the agent to approve its own report.
- Promote a fact into Graphiti without a source citation.

## Status after ingestion

On successful ingest, set:

```yaml
graphiti_ingested: true
```

The note stays in the vault as the human-readable original. Graphiti is the derived, queryable projection.

See also: [[coding-agent-rules]], [[review-gates]].
