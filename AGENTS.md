# AGENTS.md — Agent Control Plane

## Development Setup

Install all optional extras for full test coverage:

```bash
uv sync --extra rag --extra memory --extra dev --extra crypto --extra api
```

## Running Tests

Full test suite (all extras installed):

```bash
uv run pytest
```

With real sbx E2E tests (requires Docker Sandboxes installed):

```bash
ACP_RUN_REAL_SBX=1 uv run pytest
```

The 2 remaining skips in `test_context_builder.py` are intentional —
they test the fallback path when rag is NOT installed, so they skip
when rag IS installed (mutually exclusive by design).

## Linting

```bash
uv run python -m flake8 --select=F src/acp/ tests/
```

## Key Commands

- `acp run` — run a single coding task
- `acp verify` — verify evidence integrity
- `acp approve` — approve a task (human gate)
- `acp migrate` — migrate task.json files to SQLite
- `acp memory prune` — prune superseded Graphiti nodes
- `acp mission` — manage mission epics
