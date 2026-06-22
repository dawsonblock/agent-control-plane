---
type: rule
name: review-gates
status: active
applies_to: [review, all-repos]
created: 2026-06-21
---

# Review gates

What the diff reviewer checks before a report is marked ready for human approval.

## M1 checks (current)

| Check | Threshold (config) | Effect |
|-------|--------------------|--------|
| Changed file count | `review.max_changed_files` (default 20) | Exceed → high risk |
| Added line count | `review.max_added_lines` (default 1000) | Exceed → high risk |
| Auth / security file touched | `review.warn_on_auth_changes` | → at least medium risk + concern |
| Database migration touched | `review.warn_on_database_changes` | → at least medium risk + concern |
| Dependency / lockfile change | always | → at least medium risk + concern |
| Tests not modified when behavior changed | always | → concern ("behavior changed without test changes") |

## Risk levels

- **low** — small scoped change, tests pass, none of the above flags fire.
- **medium** — auth/db/dependency/config touched, or tests incomplete.
- **high** — broad diff, failing tests, risky migration, or (M5) secret-like strings.

## Recommendations

- **merge** — low risk, tests pass, scoped change.
- **revise** — medium risk, or tests incomplete, or concerns present. (Default when unsure.)
- **reject** — high risk, or a hard block fires.

## Hard blocks (auto-reject, never auto-merge)

These are added progressively. M1 implements a subset via risk heuristics; M5 adds the rest:

- secret detected *(M5)*
- main branch modified directly
- repo dirty before start
- too many unrelated files changed
- tests fail after max repair *(M4)*

## Critical property

The recommendation is **advisory**. A human always makes the final merge decision. But the risk, the concerns, and the recommendation are **always visible** in both `final_report.md` and the Obsidian note — nothing is hidden.

See also: [[coding-agent-rules]], [[memory-promotion-rules]].
