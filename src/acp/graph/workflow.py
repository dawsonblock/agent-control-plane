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

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from acp.agents.base import AgentProtocol
from acp.agents.registry import build_agent as _default_build_agent
from acp.config import DurableMode
from acp.events import EventWriter
from acp.graph.nodes import (
    NodeContext,
    auto_approve_node,
    auto_merge_node,
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
from acp.models import Event, EventType, TaskStatus
from acp.store import TaskStore
from acp.testing.runner import validation_passed, validation_ran


def node_error_handler(node_fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Wrap a graph node so unhandled exceptions produce a FAILED state.

    If the wrapped node raises, instead of crashing the graph we return a
    state patch with ``status=FAILED`` and an ``error`` message. The graph's
    conditional edges route this to the ``failed`` terminal node, which
    writes whatever evidence it can.
    """

    def wrapper(state: dict[str, Any], ctx: NodeContext) -> dict[str, Any]:
        try:
            result: dict[str, Any] = node_fn(state, ctx)
            return result
        except Exception as exc:
            # Write a node failure event directly (best effort). If the event
            # write itself fails, surface that in the state so it's not silent.
            event_write_failed = False
            try:
                ctx.events.write(
                    EventType.NODE_FAILED,
                    {
                        "node": node_fn.__name__,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    },
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
    """Route after write_report: vault note already written for all statuses.

    v0.6.0: When autonomous_mode is enabled and the task PASSED, route
    through auto_approve → auto_merge → done instead of going straight
    to done. This bypasses human approval while preserving the evidence
    trail (auto.approved and auto.merged events are hash-chained).
    """
    st = state.get("status")
    if st == TaskStatus.PASSED:
        cfg = state.get("config")
        if cfg is not None and cfg.review.autonomous_mode:
            return "auto_approve"
        return "done"
    if st == TaskStatus.NEEDS_REVIEW:
        return "needs_review"
    return "failed"


def _route_after_auto_approve(state: dict[str, Any]) -> str:
    """Route after auto_approve: merge if enabled, else done.

    If auto_merge is True, route to auto_merge. Otherwise, route
    straight to done (the task is already approved).
    If auto_approve downgraded to NEEDS_REVIEW (shouldn't happen, but
    guard against it), route to needs_review.
    """
    st = state.get("status")
    if st == TaskStatus.NEEDS_REVIEW:
        return "needs_review"
    cfg = state.get("config")
    if cfg is not None and cfg.review.auto_merge:
        return "auto_merge"
    return "done"


def _route_after_auto_merge(state: dict[str, Any]) -> str:
    """Route after auto_merge: done on success, needs_review on failure."""
    st = state.get("status")
    if st == TaskStatus.NEEDS_REVIEW:
        return "needs_review"
    return "done"


def _route_after_review(state: dict[str, Any]) -> str:
    """Route after review_diff: test-generation repair or write_report.

    v0.6.0: When dynamic_test_generation is enabled and the review flags
    TESTS_MISSING (behavior changed but no test files), route back to the
    repair loop so the agent can write tests. This is a second entry point
    into the repair loop (the first is from run_tests when commands fail).

    The repair loop cap (max_repair_attempts) still applies — the circuit
    breaker prevents infinite loops. If attempts are exhausted, fall
    through to write_report which will produce a NEEDS_REVIEW report.
    """
    cfg = state.get("config")
    review = state.get("review_result")

    # Check if TESTS_MISSING is flagged and dynamic test generation is enabled.
    if cfg is not None and cfg.agent.dynamic_test_generation:
        if review is not None and hasattr(review, "concerns"):
            tests_missing = any(
                "tests_missing" in c.lower() or "no test files" in c.lower()
                for c in review.concerns
            )
            if tests_missing:
                attempts = int(state.get("repair_attempts", 0))
                if attempts < cfg.agent.max_repair_attempts:
                    # Circuit breaker check (same as _route_after_tests).
                    breaker = getattr(cfg.agent, "repair_repeat_breaker", 0)
                    if breaker > 0 and attempts >= breaker:
                        fingerprints = state.get("repair_fingerprints", [])
                        if len(fingerprints) >= breaker:
                            recent = fingerprints[-breaker:]
                            if len(set(recent)) == 1:
                                return "write_report"
                    return "repair_plan"

    return "write_report"


def _route_after_check(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "create_worktree"


def _route_after_worktree(state: dict[str, Any]) -> str:
    return "failed" if _is_failed(state) else "build_context"


def _route_after_agent(state: dict[str, Any]) -> str:
    """Route after run_agent: failed (sentinel abort) OR run_tests (normal).

    v0.7.3: When the StreamSentinel kills the agent mid-execution (secret
    leak, strange loop, dangerous path), the node returns FAILED status.
    Route to `failed` instead of running tests/review on a partial diff
    from a killed agent.
    """
    return "failed" if _is_failed(state) else "run_tests"


def _route_after_repair(state: dict[str, Any]) -> str:
    """Route after run_repair: failed (sentinel abort) OR run_tests (normal).

    v0.7.3: Same logic as _route_after_agent, but for the repair loop.
    When the StreamSentinel kills the repair agent, route to `failed`
    instead of re-running tests on a partial repair diff.
    """
    return "failed" if _is_failed(state) else "run_tests"


def _route_after_tests(state: dict[str, Any]) -> str:
    """Route after run_tests: repair only on actual failures, else proceed.

    Repair triggers iff validation ran AND at least one non-skipped command
    failed — never on the "no validation ran" case (the old empty-list-as-pass
    behavior used to mask that and skip repair accidentally). When
    ``max_repair_attempts`` is 0, or attempts are exhausted, a failing test
    falls straight through to capture_diff → review → FAILED report.

    v0.6.0: Circuit breaker — if ``repair_repeat_breaker`` > 0 and the
    agent has produced the same failure signature that many times in a
    row, the loop stops even if attempts remain. This prevents
    hallucination loops where the agent keeps making the same mistake.
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
        # v0.6.0: Circuit breaker — check if the agent is repeating
        # the same failure. If the same fingerprint appears N times in
        # a row, stop repairing and let it fall through to review.
        breaker = getattr(cfg.agent, "repair_repeat_breaker", 0)
        if breaker > 0 and attempts >= breaker:
            fingerprints = state.get("repair_fingerprints", [])
            if len(fingerprints) >= breaker:
                # Check if the last N fingerprints are all the same.
                recent = fingerprints[-breaker:]
                if len(set(recent)) == 1:
                    return "capture_diff"
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
    def _wrap(n: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
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
    # v0.6.0: Autonomous mode nodes.
    g.add_node("auto_approve", _wrap(auto_approve_node))
    g.add_node("auto_merge", _wrap(auto_merge_node))

    # --- entry + linear happy path -------------------------------------- #
    g.add_edge(START, "create_task")
    g.add_edge("create_task", "check_repo")

    # check_repo → failed (dirty) OR create_worktree
    g.add_conditional_edges("check_repo", _route_after_check)

    # create_worktree → failed (error) OR build_context
    g.add_conditional_edges("create_worktree", _route_after_worktree)

    g.add_edge("build_context", "run_agent")
    # v0.7.3: run_agent → failed (sentinel abort) OR run_tests (normal).
    # When the StreamSentinel kills the agent mid-execution, the node
    # returns FAILED status. Route to `failed` instead of running tests
    # on a partial, potentially dangerous diff.
    g.add_conditional_edges("run_agent", _route_after_agent)

    # run_tests → repair_plan (if failing + attempts remain) OR capture_diff.
    # The repair loop: repair_plan → run_repair → run_tests (re-evaluated).
    g.add_conditional_edges("run_tests", _route_after_tests)
    g.add_edge("repair_plan", "run_repair")
    # v0.7.3: run_repair → failed (sentinel abort) OR run_tests (normal).
    # When the StreamSentinel kills the repair agent mid-execution, the
    # node returns FAILED status. Route to `failed` instead of re-running
    # tests on a partial repair diff from a killed agent.
    g.add_conditional_edges("run_repair", _route_after_repair)

    g.add_edge("capture_diff", "review_diff")
    # v0.6.0: review_diff → repair_plan (if TESTS_MISSING + dynamic_test_generation)
    # OR write_report (normal path). The conditional edge lets the repair loop
    # trigger a second time for test generation after the diff is reviewed.
    g.add_conditional_edges("review_diff", _route_after_review)

    # write_report → done (PASSED) OR needs_review OR failed
    # v0.6.0: When autonomous_mode is True and PASSED, routes through
    # auto_approve → auto_merge → done instead of straight to done.
    g.add_conditional_edges("write_report", _route_after_write_report)

    # v0.6.0: Autonomous mode edges.
    # auto_approve → auto_merge (if auto_merge) OR done
    g.add_conditional_edges("auto_approve", _route_after_auto_approve)
    # auto_merge → done (success) OR needs_review (merge conflict)
    g.add_conditional_edges("auto_merge", _route_after_auto_merge)

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
    task_id: str | None = None,
    mission_id: str = "",
    mission_step_index: int = -1,
    parent_task_id: str = "",
    recursion_depth: int = 0,
) -> dict[str, Any]:
    """Build + invoke the graph once and return the final state.

    Handles the placeholder-writer setup: the EventWriter is constructed with
    a sentinel id, and the ``create_task`` node relocates it to the real run
    dir once the task id is minted. Returns the graph's final state dict.

    When ``config.evidence.signing_key_path`` is set, events are Ed25519-signed.
    When ``config.evidence.durable_store`` is set, events are dual-written to
    a SQLite database in addition to the JSONL log.

    v0.7.0 (M14): When ``mission_id`` and ``parent_task_id`` are provided,
    the task is linked to a mission step. The ``evidence.finalized`` event
    will include ``parent_artifact_hash`` — the sha256 of the preceding
    step's ``diff.patch`` — proving sequential generation.
    """
    # v0.7.0 (Phase 1.1): Wire SQLite-as-primary if configured.
    # Only initialize the durable task store when durable_mode is not
    # DISABLED — respecting the operator's explicit choice to skip SQLite.
    evidence_cfg = getattr(config, "evidence", None)
    durable_task_store = None
    if (
        evidence_cfg
        and evidence_cfg.durable_store
        and evidence_cfg.durable_mode != DurableMode.DISABLED
    ):
        from acp.evidence.durable_task_store import DurableTaskStore

        durable_task_store = DurableTaskStore(evidence_cfg.durable_store)
        durable_task_store.init()
    store = TaskStore(
        runs_root=runs_root,
        durable_store=durable_task_store,
        primary=evidence_cfg.task_store_primary if evidence_cfg else "json",
    )
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
    #
    # Durable mode controls failure behavior:
    #   - disabled: no SQLite writes (durable_store path is ignored)
    #   - best_effort: failures are recorded as warnings, run continues
    #   - required: failures are fatal — the run cannot succeed
    durable_store = None
    durable_store_failures: list[str] = []
    durable_mode = "best_effort"
    if evidence_cfg:
        durable_mode = (
            evidence_cfg.durable_mode.value
            if hasattr(evidence_cfg.durable_mode, "value")
            else str(evidence_cfg.durable_mode)
        )

    if evidence_cfg and evidence_cfg.durable_store and durable_mode != "disabled":
        try:
            from acp.evidence.durable_store import DurableEventStore

            durable_store = DurableEventStore(evidence_cfg.durable_store)
            durable_store.init()
            # Wrap the write method to dual-write. In best_effort mode, failures
            # are recorded but don't crash the run. In required mode, failures
            # raise — the run cannot succeed without durable evidence.
            original_write = events.write

            def _dual_write(type: EventType, payload: dict[str, Any] | None = None) -> Event:
                evt = original_write(type, payload)
                try:
                    durable_store.append(evt)
                except Exception as exc:  # noqa: BLE001
                    durable_store_failures.append(f"{evt.event_id} ({evt.type.value}): {exc}")
                    if durable_mode == "required":
                        raise  # fail closed — durable evidence is required
                return evt

            events.write = _dual_write  # type: ignore[method-assign]
        except Exception as exc:
            durable_store_failures.append(f"init: {exc}")
            if durable_mode == "required":
                from acp.errors import EvidenceConfigError

                raise EvidenceConfigError(
                    f"durable store initialization failed (required mode): {exc}"
                ) from exc

    wf = build_workflow(store=store, events=events, agent_factory=agent_factory)

    state = {
        "config": config,
        "user_request": user_request,
        "vault_root": Path(vault_root),
        "runs_root": Path(runs_root),
    }
    if task_id is not None:
        state["preallocated_task_id"] = task_id
    # v0.7.0 (M14): Mission context for cross-task artifact sharing.
    if mission_id:
        state["mission_id"] = mission_id
        state["mission_step_index"] = mission_step_index
        state["parent_task_id"] = parent_task_id
    # v0.7.4: Subtask recursion depth — prevents agent fork bombs.
    state["recursion_depth"] = recursion_depth
    result: dict[str, Any] = wf.invoke(state, config={"configurable": {"thread_id": "acp-run"}})

    # Close the durable stores if they were opened.
    if durable_store is not None:
        durable_store.close()
    if durable_task_store is not None:
        durable_task_store.close()

    # Surface durable-store failures to the caller. The JSONL log is canonical,
    # so the run can succeed, but the operator must know the durable index is
    # incomplete — silent evidence loss is unacceptable in a trust system.
    if durable_store_failures:
        result["durable_store_warnings"] = durable_store_failures

    return result
