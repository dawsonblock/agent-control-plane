"""The compiled ACP LangGraph workflow.

Wires the node adapters from ``nodes.py`` into a ``StateGraph`` with
conditional edges that route failures to the ``failed`` node. The happy
path is linear:

    START → create_task → check_repo → create_worktree → build_context
         → run_agent → run_tests → capture_diff → review_diff
         → write_report → done → END

There is **no separate ``write_vault_note`` node**: vault-note writing
happens inside ``write_report_node`` (for every status), so the report and
the vault note always come from the same render. ``write_report`` then
routes to ``done`` (PASSED), ``needs_review`` (NEEDS_REVIEW), or ``failed``
(FAILED).

Failure short-circuits route to ``failed`` instead. The ``failed`` node
still writes a report when it can (spec rule: a failed task produces an
evidence report) — except for the pre-worktree dirty-repo case, where
nothing exists to report on yet.

The M4 repair loop branches off ``run_tests``: when validation ran and a
non-skipped command failed (and attempts remain), ``run_tests`` routes to
``repair_plan → run_repair → run_tests`` (re-evaluated). The cap at
``config.agent.max_repair_attempts`` guarantees termination.

Compiled with an in-memory ``MemorySaver`` checkpointer so runs are
inspectable. (Durable checkpointing is a later concern; M3 only needs the
graph to be drivable and its transitions observable.)
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from acp.agents.base import AgentProtocol
from acp.agents.registry import build_agent as _default_build_agent
from acp.events import EventWriter
from acp.graph.nodes import (
    NodeContext,
    build_context_node,
    capture_diff_node,
    check_repo,
    create_task,
    create_worktree_node,
    done_node,
    failed_node,
    needs_review_node,
    repair_plan_node,
    review_diff_node,
    run_agent_node,
    run_repair_agent_node,
    run_tests_node,
    write_report_node,
)
from acp.graph.state import ACPState
from acp.models import EventType, TaskStatus
from acp.store import TaskStore
from acp.testing.runner import validation_passed, validation_ran


def node_error_handler(node_fn: Callable) -> Callable:
    """Wrap a graph node so unhandled exceptions produce a FAILED state.

    If the wrapped node raises, instead of crashing the graph we return a
    state patch with ``status=FAILED`` and an ``error`` message. The graph's
    conditional edges route this to the ``failed`` terminal node, which
    writes whatever evidence it can.
    """

    def wrapper(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
        try:
            return node_fn(state, ctx)
        except Exception as exc:
            # Write a node failure event directly (best effort). If the event
            # write itself fails, surface that in the state so it's not silent.
            event_write_failed = False
            try:
                ctx.events.write(
                    EventType.NODE_FAILED,
                    {"node": node_fn.__name__, "exception_type": type(exc).__name__, "message": str(exc)},
                )
            except Exception:  # noqa: BLE001
                event_write_failed = True
            patch: dict[str, Any] = {
                "status": TaskStatus.FAILED,
                "error": f"{node_fn.__name__}: {exc}",
            }
            if event_write_failed:
                patch["node_failed_event_write_failed"] = True
            return patch

    return wrapper


def _is_failed(state: dict[str, Any]) -> bool:
    """Conditional-edge router: did the preceding node mark the run failed?"""
    return state.get("status") == TaskStatus.FAILED


def _needs_review(state: dict[str, Any]) -> bool:
    """Check if the run ended with ``NEEDS_REVIEW``."""
    return state.get("status") == TaskStatus.NEEDS_REVIEW


def _route_after_write_report(state: dict[str, Any]) -> str:
    """Route after write_report: vault note already written for all statuses."""
    st = state.get("status")
    if st == TaskStatus.PASSED:
        return "done"
    if st == TaskStatus.NEEDS_REVIEW:
        return "needs_review"
    return "failed"


def _route_after_check(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "create_worktree"


def _route_after_worktree(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "build_context"


def _route_after_tests(state: dict[str, Any]) -> str:
    """Route after run_tests: repair only on actual failures, else proceed.

    Repair triggers iff validation ran AND at least one non-skipped command
    failed — never on the "no validation ran" case (``all_passed([])`` used
    to mask that as a pass and skip repair accidentally). When
    ``max_repair_attempts`` is 0, or attempts are exhausted, a failing test
    falls straight through to capture_diff → review → FAILED report.
    """
    cfg = state.get("config")
    results = state.get("command_results", [])
    # Actual failures only: validation ran but not everything passed.
    has_failures = validation_ran(results) and not validation_passed(results)
    if not has_failures:
        return "capture_diff"
    if cfg is None:
        return "capture_diff"
    attempts = int(state.get("repair_attempts", 0))
    if attempts < cfg.agent.max_repair_attempts:
        return "repair_plan"
    return "capture_diff"


def build_workflow(
    *,
    store: TaskStore,
    events: EventWriter,
    agent_factory: Callable[[Any], Any] | None = None,
) -> Any:
    """Build + compile the ACP workflow graph.

    The ``store`` and ``events`` are bound to each node via ``NodeContext``
    so every node shares the same run dir + event log. The ``events`` writer
    may be constructed with a placeholder task id; the ``create_task`` node
    relocates it to the real run dir once the id is minted.

    ``agent_factory`` is optional and defaults to the registry's
    ``build_agent``; tests inject a controllable agent to exercise the
    repair loop deterministically.

    Returns a compiled graph ready to ``.invoke(initial_state)``.
    """
    ctx = NodeContext(
        store=store,
        events=events,
        agent_factory=agent_factory or _default_build_agent,
    )

    g = StateGraph(ACPState)

    # Wrap every node with the error handler so unhandled exceptions produce
    # a FAILED state instead of crashing the graph.
    def _wrap(n: Callable) -> Callable:
        return partial(node_error_handler(n), ctx=ctx)

    # Bind ctx into each node so LangGraph sees a single-arg callable.
    g.add_node("create_task", _wrap(create_task))
    g.add_node("check_repo", _wrap(check_repo))
    g.add_node("create_worktree", _wrap(create_worktree_node))
    g.add_node("build_context", _wrap(build_context_node))
    g.add_node("run_agent", _wrap(run_agent_node))
    g.add_node("run_tests", _wrap(run_tests_node))
    g.add_node("capture_diff", _wrap(capture_diff_node))
    g.add_node("review_diff", _wrap(review_diff_node))
    g.add_node("write_report", _wrap(write_report_node))
    g.add_node("done", _wrap(done_node))
    g.add_node("failed", _wrap(failed_node))
    g.add_node("needs_review", _wrap(needs_review_node))
    # M4 repair loop.
    g.add_node("repair_plan", _wrap(repair_plan_node))
    g.add_node("run_repair", _wrap(run_repair_agent_node))

    # --- entry + linear happy path -------------------------------------- #
    g.add_edge(START, "create_task")
    g.add_edge("create_task", "check_repo")

    # check_repo → failed (dirty) OR create_worktree
    g.add_conditional_edges("check_repo", _route_after_check)

    # create_worktree → failed (error) OR build_context
    g.add_conditional_edges("create_worktree", _route_after_worktree)

    g.add_edge("build_context", "run_agent")
    g.add_edge("run_agent", "run_tests")

    # run_tests → repair_plan (if failing + attempts remain) OR capture_diff.
    # The repair loop: repair_plan → run_repair → run_tests (re-evaluated).
    g.add_conditional_edges("run_tests", _route_after_tests)
    g.add_edge("repair_plan", "run_repair")
    g.add_edge("run_repair", "run_tests")

    g.add_edge("capture_diff", "review_diff")
    g.add_edge("review_diff", "write_report")

    # write_report → done (PASSED) OR needs_review OR failed
    g.add_conditional_edges("write_report", _route_after_write_report)

    # Terminal nodes.
    g.add_edge("done", END)
    g.add_edge("failed", END)
    g.add_edge("needs_review", END)

    return g.compile(checkpointer=MemorySaver())


# --------------------------------------------------------------------------- #
# Convenience runner — used by the CLI (and tests).
# --------------------------------------------------------------------------- #

def run_workflow(
    *,
    config: Any,
    user_request: str,
    runs_root: Path | str,
    vault_root: Path | str,
    agent_factory: Callable[[Any], AgentProtocol] | None = None,
) -> dict[str, Any]:
    """Build + invoke the graph once and return the final state.

    Handles the placeholder-writer setup: the EventWriter is constructed with
    a sentinel id, and the ``create_task`` node relocates it to the real run
    dir once the task id is minted. Returns the graph's final state dict.

    When ``config.evidence.signing_key_path`` is set, events are Ed25519-signed.
    When ``config.evidence.durable_store`` is set, events are dual-written to
    a SQLite database in addition to the JSONL log.
    """
    store = TaskStore(runs_root=runs_root)
    # Ensure vault_root exists — the workflow writes a vault note at the end
    # and the vault directory must be present by then.
    Path(vault_root).mkdir(parents=True, exist_ok=True)
    # Placeholder writer — create_task will relocate it to the real run dir.
    events = EventWriter("__pending__", store.root / "__pending__")

    # Wire Ed25519 signing if a signing key is configured. Signing is a trust
    # mode: if it is configured, failure to sign must be FATAL. We never
    # silently downgrade configured signed evidence to unsigned evidence —
    # that is exactly the kind of integrity gap this control plane exists to
    # prevent. A missing key file, a malformed key, or an unavailable
    # `cryptography` package all raise EvidenceConfigError (fail closed).
    evidence_cfg = getattr(config, "evidence", None)
    if evidence_cfg and evidence_cfg.signing_key_path:
        from acp.errors import EvidenceConfigError
        try:
            key_bytes = evidence_cfg.signing_key_path.read_bytes()
        except OSError as exc:
            raise EvidenceConfigError(
                f"signing key file not readable: {evidence_cfg.signing_key_path} ({exc})"
            ) from exc
        if len(key_bytes) != 32:
            raise EvidenceConfigError(
                f"signing key file must be exactly 32 bytes, got {len(key_bytes)}: "
                f"{evidence_cfg.signing_key_path}"
            )
        try:
            events.set_signing_key(key_bytes)
        except ImportError as exc:
            raise EvidenceConfigError(
                "signing is configured but the 'cryptography' package is not "
                "installed — refusing to run unsigned. Install with: uv sync --extra crypto"
            ) from exc

    # Wire SQLite durable store if configured. The store is additive: events
    # go to both JSONL (canonical) and SQLite (queryable index). We wrap the
    # EventWriter's write method to dual-write.
    durable_store = None
    durable_store_failures: list[str] = []
    if evidence_cfg and evidence_cfg.durable_store:
        try:
            from acp.evidence.durable_store import DurableEventStore
            durable_store = DurableEventStore(evidence_cfg.durable_store)
            durable_store.init()
            # Wrap the write method to dual-write. Failures are recorded but
            # don't crash the run — the JSONL log is canonical. However, they
            # are NOT silently swallowed: the failure count is surfaced after
            # the run so the operator knows the durable index is incomplete.
            original_write = events.write
            def _dual_write(type, payload=None):
                evt = original_write(type, payload)
                try:
                    durable_store.append(evt)
                except Exception as exc:  # noqa: BLE001
                    durable_store_failures.append(
                        f"{evt.event_id} ({evt.type.value}): {exc}"
                    )
                return evt
            events.write = _dual_write  # type: ignore[method-assign]
        except Exception as exc:
            # Store initialization failed — record it but don't crash.
            # The run can proceed with JSONL-only durability.
            durable_store_failures.append(f"init: {exc}")

    wf = build_workflow(store=store, events=events, agent_factory=agent_factory)

    state = {
        "config": config,
        "user_request": user_request,
        "vault_root": Path(vault_root),
        "runs_root": Path(runs_root),
    }
    result = wf.invoke(state, config={"configurable": {"thread_id": "acp-run"}})

    # Close the durable store if it was opened.
    if durable_store is not None:
        durable_store.close()

    # Surface durable-store failures to the caller. The JSONL log is canonical,
    # so the run can succeed, but the operator must know the durable index is
    # incomplete — silent evidence loss is unacceptable in a trust system.
    if durable_store_failures:
        result["durable_store_warnings"] = durable_store_failures

    return result
