---
type: rule
name: coding-agent-rules
status: active
applies_to: [all-agents, all-repos]
created: 2026-06-21
---

# Coding agent rules

Non-negotiable constraints on any coding agent run through ACP. These are enforced by the control plane, not by the agent's goodwill.

## Never

- Edit files outside the assigned worktree.
- Commit to, push, or rebase the repo's default branch (`main`).
- Modify `.env`, `*.key`, `*.pem`, or any file matching the repo's secret exclude globs.
- Delete or rewrite test files to make them pass.
- Disable linters, type-checkers, or CI configuration.
- Introduce dependencies without updating the lockfile.
- Force-push, rewrite history, or amend commits outside the task branch.
- Promote its own report to memory. (Only humans approve.)

## Always

- Keep changes minimal and scoped to the task.
- Add or update tests when changing behavior.
- Leave the worktree in a state where `test` / `build` / `lint` can run.
- Stop and report if the task is ambiguous, impossible, or would require touching unrelated systems.

## How ACP enforces these

- Worktree isolation means there is no path to `main` from inside a run.
- Dirty-repo detection means the agent can't ride on uncommitted human changes.
- The reviewer flags auth/db/dependency/lockfile/test changes before any approval.
- Reports ship with `approved: false`; nothing is remembered until a human flips it.

See also: [[worktree-safety]], [[review-gates]], [[memory-promotion-rules]].
