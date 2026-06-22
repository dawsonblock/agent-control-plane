# Safety model

ACP exists to make coding agents **safer**. Safety here is not a feature added later; it's the spine. Every design choice below exists to constrain what an agent can do and to make anything it *does* do visible and reversible.

## Invariants (must never be violated)

1. **Main is never touched.** ACP works only in worktrees on `agent/<task_id>` branches. A commit to `main` by ACP is a critical bug.
2. **Dirty repos fail fast.** If the target repo has uncommitted changes when a task starts, ACP refuses to proceed rather than mix agent work with human work.
3. **Nothing is asserted without evidence.** Reports and events are truth; memory is derived. A claim in Graphiti without a backing artifact + approved note is garbage-collected.
4. **No autonomous merge.** A human must approve every note (`approved: true`) before it can promote into memory. The system never promotes itself.
5. **Agents are workers.** An agent edits files in one worktree and returns. It cannot decide to merge, push, edit main, or promote memory.

## Isolation

```
<repo> (main, untouched)
  └─ worktree at data/runs/<task_id>/worktree   (branch agent/<task_id>)
```

The worktree is the agent's entire universe. All commands run with `cwd = worktree_path`. The agent has no path to main except through a human-initiated merge.

## Review gates (M1 subset; full taxonomy in M5)

M1's reviewer flags, before any human approval:

- changed-file count vs `review.max_changed_files`
- added-line count vs `review.max_added_lines`
- auth / security file paths touched
- database migration paths touched
- dependency / lockfile changes
- whether tests were modified alongside behavior changes

These produce a `risk` (low/medium/high) and `recommendation` (merge/revise/reject). The recommendation is advisory — a human always makes the final call — but it is **always visible** in the report and the Obsidian note.

M5 adds: secret-like string scanning, `.env` change detection, large generated file detection, direct main-branch modification detection. See `vault/rules/review-gates.md`.

## The approval gate

```
run produces final_report.md
  → copied to vault/tasks/<task_id>.md with approved: false, memory_status: draft
  → human reads it in Obsidian
  → human sets approved: true (only if satisfied)
  → only THEN may memory promotion (M7) consider it
```

A note with `approved: false` can never become memory. This is the single most important safety property: **the system cannot gaslight itself**, because every fact it remembers was first read and approved by a human.

## Reversibility

- Worktrees are removable; branches are deletable. Nothing ACP does to git is irreversible.
- Obsidian notes are markdown files under git. A bad note can be edited or deleted.
- Graphiti (M7) is derived: it can be rebuilt from the approved vault notes. Wiping it loses nothing that isn't reconstructable.

## What ACP does not do (yet)

- Run untrusted code in a sandbox. (M13 — OpenHands/Superserve.) Until then, agents run with the user's full local privileges, which is why isolation is *git-level* (worktrees) not *OS-level*.
- Detect secrets exhaustively. M1 uses path heuristics; M5 adds string scanning. Treat M1's secret detection as a tripwire, not a guarantee.
