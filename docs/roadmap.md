# Roadmap

The non-negotiable rule across every milestone: **no new layer until the previous layer produces evidence.** A milestone is "done" only when its gate passes on a real run, not when its code is written.

## Current build (this repo, now): v0.5.8 alpha

| Milestone | Delivers | Status |
|-----------|----------|--------|
| **M0 — Scaffold** | Folders, `pyproject`, configs, vault skeleton, docs | `import acp` succeeds |
| **M1 — Manual evidence loop** | `acp run` runs one task in a worktree, captures everything, writes report + Obsidian note | E2E test passes; main untouched |
| **M2 — Generic CLI agent adapter** | Swap the coder agent via config (`shell` ↔ `custom` + `command_template`) | Agent swap test passes |
| **M3 — LangGraph state machine** | Linear CLI refactored into a graph; failed nodes visible; failed tasks still write reports | Graph test passes |
| **M4 — Repair loop** | Failing tests trigger ≤ `max_repair_attempts` repair rounds | No infinite loops |
| **M5 — Review hardening** | `secret_scanner.py` + `risk.py`; full risk taxonomy; `GateResult` artifact | Risky diffs flagged; gate-correct status |
| **v0.5.x — Dogfood hardening** | `--legacy` removed, hash-chained event log, evidence manifest, `acp cleanup`, CI workflow, early-failure evidence, CLI output honesty | Stable |
| **v0.5.6 — Trust layer** | fsync'd event writes, Ed25519 event signing, event timeline in report, SQLite durable event store | Stable |
| **v0.5.7 — Trust CLI** | Config-driven signing + durable store, `acp verify` + `acp events` commands, durable task store | Stable |
| **v0.5.8 — Approval workflow** | `acp approve`, `acp reject`, `acp list`, vault note audit trail, human.approved/rejected events | Current |

## Downstream (deferred — do not start before the gate above passes)

| Milestone | Delivers | Gate |
|-----------|----------|------|
| **M6 — Haystack retrieval** | `context_bundle.md` generated before agent run | Context includes relevant files + prior reports |
| **M7 — Graphiti memory** | Approved notes → temporal graph of verified facts | System retrieves prior verified facts before new tasks |
| **M8 — Skills governance** | YAML playbooks drive review/repair/promotion | Each major action has a named skill |
| **M9 — Agent File registry** | Versioned, hashed, role-limited agent profiles | Unapproved / hash-mismatched agents cannot load |
| **M10 — FastAPI** | Local HTTP API over the workflow | CLI workflow controllable via endpoints |
| **M11 — React UI** | Browser control of create/run/review/approve | Full task lifecycle without terminal |
| **M12 — Desktop wrapper** | Mac app (Tauri/Electron later) | Usable as a local command center |
| **M13 — OpenHands / Superserve** | Optional sandbox execution backends | (not v0.1) |
| **M14 — Mission dashboard** | Fusion-style missions/projects/tasks | (not v0.1) |

## What we explicitly ignore until the spine works

multi-agent free-chat · autonomous merge · mobile · cloud · plugin SDK · marketplace · microVM sandboxing · Letta runtime · MemoryBear · TencentDB memory · Fusion fork · full Maestro fork · agent self-improvement · automatic memory ingestion from all chats

These are distractions until the evidence loop is trustworthy.
