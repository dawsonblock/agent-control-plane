# Security Policy

## Supported versions

ACP is under active hardening and should be used for controlled dogfooding,
not production autonomous operation. Security fixes are applied to the latest
`master` branch only.

| Version | Supported |
|---------|-----------|
| latest (`master`) | yes |
| older releases | no |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately:

1. Open a **private security advisory** on GitHub
   (Security tab → "Report a vulnerability"), or
2. Email the maintainer directly with a description, reproduction steps,
   and impact assessment.

You will receive an acknowledgement within 72 hours. Please allow reasonable
time for a fix to be issued before any public disclosure.

## Security model

ACP is a **local control plane for coding agents**. Its security model rests
on these invariants:

### Isolation

- Every coding task runs in an isolated git worktree (`gitops/worktrees.py`).
- Optional sandbox executors (`docker_sbx`, `gvisor`) provide OS-level
  isolation for the agent process.
- The default `cli` executor runs in a worktree only — no sandbox. Use a
  sandbox executor for untrusted agents.

### Evidence integrity

- All actions are recorded in a hash-chained event log (`events.py`).
- Optional Ed25519 signing (`crypto` extra) provides authenticity proofs.
- `acp verify` checks the hash chain and signatures; tampering fails
  verification.

### Secret protection

- `review/secret_scanner.py` scans diffs for leaked secrets before any
  report is written or merge is performed.
- TruffleHog integration (when installed) verifies whether detected keys
  are live before flagging them.
- `block_secret_leaks: true` hard-blocks the workflow on detected secrets.

### Human firewall

- By default, every task requires explicit human approval (`acp approve`)
  before its branch can be merged.
- **Autonomous mode** (`review.autonomous_mode: true`) bypasses human
  approval. Only enable it with a sandbox executor, `block_secret_leaks:
  true`, and `network_policy: locked_down`.
- `auto_merge_max_risk` (default `medium`) prevents auto-merge of
  HIGH-risk changes (database, secrets, auth) even in autonomous mode.
- The event-chain integrity gate refuses to auto-merge if the signed audit
  trail is broken.

### Egress control

- `egress.py` proxies outbound network traffic for sandboxed agents.
- `network_policy: locked_down` blocks all non-essential egress.

### Path safety

- Task IDs are validated against `task_<YYYYMMDD>_<NNNN>` to prevent path
  traversal (`store.is_valid_task_id`).
- Mission IDs are validated similarly.
- YAML is always loaded with `yaml.safe_load` (no arbitrary object
  deserialization).

### Subprocess safety

- `CLIAgent` uses `shlex.split()` by default and refuses shell
  metacharacters in worktree mode unless `agent.allow_shell: true` is set.
- Sandbox executors run commands inside the isolated environment.

## Known limitations

- The `cli` executor without a sandbox executor does not provide OS-level
  isolation. A malicious agent with `allow_shell: true` can execute
  arbitrary commands on the host.
- Autonomous mode removes the human firewall. Misconfigured gates could
  allow unintended auto-merges. Always set `auto_merge_max_risk` and
  `block_secret_leaks` when enabling it.
- The FastAPI server (`acp serve`) binds to `127.0.0.1` by default. Do
  not expose it to untrusted networks without additional auth.
