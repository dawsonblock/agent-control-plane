# Roadmap

The non-negotiable rule across every milestone: **no new layer until the previous layer produces evidence.** A milestone is "done" only when its gate passes on a real run, not when its code is written.

## Current build (this repo, now): v0.7.1 alpha

| Milestone | Delivers | Status |
|-----------|----------|--------|
| **M0 — Scaffold** | Folders, `pyproject`, configs, vault skeleton, docs | Stable |
| **M1 — Manual evidence loop** | `acp run` runs one task in a worktree, captures everything, writes report + Obsidian note | Stable |
| **M2 — Generic CLI agent adapter** | Swap the coder agent via config (`shell` ↔ `custom` + `command_template`) | Stable |
| **M3 — LangGraph state machine** | Linear CLI refactored into a graph; failed nodes visible; failed tasks still write reports | Stable |
| **M4 — Repair loop** | Failing tests trigger ≤ `max_repair_attempts` repair rounds | Stable |
| **M5 — Review hardening** | `secret_scanner.py` + `risk.py`; full risk taxonomy; `GateResult` artifact | Stable |
| **v0.5.x — Dogfood hardening** | `--legacy` removed, hash-chained event log, evidence manifest, `acp cleanup`, CI workflow, early-failure evidence, CLI output honesty | Stable |
| **v0.5.6 — Trust layer** | fsync'd event writes, Ed25519 event signing, event timeline in report, SQLite durable event store | Stable |
| **v0.5.7 — Trust CLI** | Config-driven signing + durable store, `acp verify` + `acp events` commands, durable task store | Stable |
| **v0.5.8 — Approval workflow** | `acp approve`, `acp reject`, `acp list`, vault note audit trail, human.approved/rejected events | Stable |
| **v0.5.9–v0.5.16** | Approval-safe evidence, evidence binding, lifecycle transaction integrity, Docker Sandboxes executor, pure-projection vault notes, TruffleHog, sandbox evidence semantics, executor evidence binding | Stable |
| **v0.6.0 — Autonomous mode** | `review.autonomous_mode` + `auto_merge`; `auto.approved`/`auto.merged` events; dynamic test generation; repair circuit breaker | Stable |
| **v0.6.1 — M6 Haystack RAG** | `context_bundle.md` generated before agent run; graceful fallback when `rag` extra not installed | Stable |
| **v0.6.2 — M7 Graphiti memory** | Approved notes → temporal graph of verified facts; `acp memory promote`/`search`; auto-promotion | Stable |
| **v0.6.3 — M8 Skills governance** | YAML playbooks drive review/repair/promotion; hard blocks, risk elevators, required files | Stable |
| **v0.6.4 — M9 Agent File registry** | Versioned, hashed, role-limited agent profiles; SHA-256 hash verification | Stable |
| **v0.6.5 — M10 FastAPI** | Local HTTP API over the workflow; `acp serve`; SSE streaming; orphan recovery | Stable |
| **v0.6.6 — M11 React UI** | Vite + React + TypeScript dashboard; TaskList/TaskDetail/RunForm/MemorySearch | Stable |
| **v0.6.7 — Polish** | Shared lifecycle service, `shell=True` safety, SSE streaming, DurableTaskStore orphan recovery, CORS | Stable |
| **v0.6.8 — Human firewall** | `auto_merge_max_risk` ceiling; event-chain integrity gate before auto-merge; `auto.merge.refused` event | Stable |
| **v0.6.9 — Federation + cognitive memory** | MCP federation (JSON-RPC stdio, no `mcp` dep); cognitive memory tiers (working/episodic/semantic) | Stable |
| **v0.7.0 — M14 Mission layer** | `MissionStore`, `Mission`/`MissionStep`/`MissionStatus`; `acp mission` CLI; mission event log | Stable |
| **v0.7.1 — Engineering execution plan** | Tech debt fixes, `DurableTaskStore.check_integrity()`, `GvisorExecutor`, `custom_secret_regexes`, React UI missions/skills, Tauri desktop wrapper, DurableTaskStore promoted to Stable | Current |

All core milestones (M0–M11) are complete.

## Downstream (deferred — do not start before the current gate passes)

| Milestone | Delivers | Gate |
|-----------|----------|------|
| **M12 — Desktop wrapper hardening** | Mac app (Tauri) packaging, signed builds, auto-update | Tauri build passes in CI |
| **M13 — OpenHands / Superserve** | Optional sandbox execution backends | Executor protocol test passes |
| **M15 — Mission dashboard** | Fusion-style missions/projects/tasks UI | Full mission lifecycle in browser |

## What we explicitly ignore until the spine works

multi-agent free-chat · autonomous merge without gates · mobile · cloud · plugin SDK · marketplace · microVM sandboxing beyond sbx/gvisor · Letta runtime · MemoryBear · TencentDB memory · Fusion fork · full Maestro fork · agent self-improvement · automatic memory ingestion from all chats

These are distractions until the evidence loop is trustworthy.
