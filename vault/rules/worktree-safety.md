---
type: rule
name: worktree-safety
status: active
applies_to: [gitops, all-repos]
created: 2026-06-21
---

# Worktree safety

How ACP isolates agent work from the human's working repo.

## Invariant

> The repo's default branch HEAD is unchanged after every ACP run.

This is verified by every run's acceptance gate. If it ever fails, the run is treated as a critical incident.

## Procedure

1. **Pre-check.** Before creating anything, verify the repo is clean (`git status` empty on the default branch). If dirty → **fail fast**, write a `task.failed` event, do not create a worktree.
2. **Branch.** Create `agent/<task_id>` from the default branch's current HEAD.
3. **Worktree.** `git worktree add data/runs/<task_id>/worktree <branch>`.
4. **Run.** All agent commands execute with `cwd = worktree_path`. The agent never sees a path to `main`.
5. **Capture.** Diff is taken against the base branch HEAD recorded at step 2.
6. **Cleanup (optional, human-triggered).** `git worktree remove` + branch delete. Never automatic — the human may want to inspect the worktree.

## Never

- Create a worktree from a dirty base.
- Allow the agent to operate outside its worktree.
- Auto-delete a worktree after a run. (Inspection is a legitimate review step.)
- Reuse a worktree across tasks. One task, one worktree, one branch.

## Failure modes

| Symptom | ACP response |
|---------|--------------|
| Repo dirty at start | Fail fast; no worktree created |
| Worktree path already exists | Fail; likely a stale run — human cleans up |
| Branch name collision | Fail; task ID generation should prevent this |
| Worktree creation errors | `task.failed` event; no partial state left in main |

See also: [[coding-agent-rules]], [[review-gates]].
