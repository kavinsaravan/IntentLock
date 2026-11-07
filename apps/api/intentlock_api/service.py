"""Authorization orchestration shared by the HTTP endpoint and the Lambda handler.

Loads authoritative state, expires overdue runs, reserves the call slot atomically, records the
event BEFORE deciding, runs the pure deterministic policy, records the decision, and commits a
stop transition. Never imports or calls Bedrock.
"""
import json
import logging
from datetime import datetime
from typing import Any, Callable

from intentlock_policy import authorize
from intentlock_policy.models import AuthorizationDecision, Run
from intentlock_policy.redact import redact_arguments

from .store import now as _now

audit_log = logging.getLogger("intentlock.audit")
NowFn = Callable[[], datetime]


def expire_if_needed(store, run_id: str, now_fn: NowFn = _now) -> Run | None:
    """Mark an overdue active run inactive (once, atomically) BEFORE anything else reads it."""
    run = store.get_run(run_id)
    if run is None or run.status != "active" or run.started_at is None:
        return run
    c = store.get_contract(run.contract_id, run.contract_version)
    if c is None:
        return run
    t = now_fn()
    if (t - run.started_at).total_seconds() >= c.limits.run_timeout_seconds:
        won = store.transition_run(run_id, ["active"], status="stopped", stopped_at=t, stop_reason="run_timeout")
        if won is not None:  # exactly one racer records the event
            store.append_event(run_id, event_type="run_stopped", status="recorded",
                               message="Run time limit reached",
                               details={"status": "stopped", "cause": "run_timeout",
                                        "run_timeout_seconds": c.limits.run_timeout_seconds})
        return store.get_run(run_id)
    return run


def stop_run_state(store, run_id: str, status: str, reason: str, now_fn: NowFn = _now) -> Run | None:
    """Commit the state change (atomic compare-and-set) and record it. First stopper wins."""
    won = store.transition_run(run_id, ["created", "active"], status=status, stopped_at=now_fn(), stop_reason=reason)
    if won is not None:
        store.append_event(run_id, event_type="run_stopped", status="recorded", message=reason,
                           details={"status": status})
        return won
    return store.get_run(run_id)


def authorize_request(raw: Any, store, now_fn: NowFn = _now) -> AuthorizationDecision:
    """Full authoritative authorization of one tool request. Always returns a decision."""
    raw_dict = raw if isinstance(raw, dict) else None
    run_id = raw_dict.get("run_id") if raw_dict else None
    run = expire_if_needed(store, run_id, now_fn) if isinstance(run_id, str) and run_id else None
    contract = store.get_contract(run.contract_id, run.contract_version) if run else None
    prior = store.reserve_tool_call(run.run_id) if run else 0
    ev = None
    if run is not None and raw_dict is not None:  # event exists BEFORE the decision (FR-4)
        tool = raw_dict.get("tool") if isinstance(raw_dict.get("tool"), str) else None
        try:
            red = redact_arguments(tool or "", raw_dict.get("arguments"))
        except Exception:
            red = {"_invalid": True}
        rid = raw_dict.get("request_id") if isinstance(raw_dict.get("request_id"), str) else None
        ev = store.append_event(run.run_id, event_type="tool_request", status="pending",
                                request_id=rid, tool=tool, arguments_redacted=red)
    decision = authorize(raw_dict if raw_dict is not None else {}, run, contract, prior, now_fn())
    if decision.decision == "allow" and run is not None:
        # Close the TOCTOU window: if the run was stopped while we evaluated, do not allow.
        fresh = store.get_run(run.run_id)
        if fresh is None or fresh.status != "active":
            decision = AuthorizationDecision(
                request_id=decision.request_id, decision="deny", reason_code="RUN_NOT_ACTIVE",
                message="The run is not active.", evidence={"run_status": fresh.status if fresh else "missing"},
                stop_run=False, latency_ms=decision.latency_ms)
    if ev is not None and run is not None:
        store.update_event(run.run_id, ev.seq, status="decided", decision=decision.decision,
                           reason_code=decision.reason_code, message=decision.message,
                           latency_ms=decision.latency_ms, evidence=decision.evidence)
        if decision.stop_run:
            stop_run_state(store, run.run_id, "blocked", f"{decision.reason_code}: request {ev.request_id}", now_fn)
    audit_log.info(json.dumps({"event": "authorization", "request_id": decision.request_id,
                               "run_id": run_id if isinstance(run_id, str) else None,
                               "decision": decision.decision, "reason_code": decision.reason_code,
                               "stop_run": decision.stop_run, "latency_ms": decision.latency_ms}))
    return decision


def error_decision(request_id: str, code: str = "POLICY_ENGINE_ERROR") -> AuthorizationDecision:
    """Fail-closed decision for infrastructure failures (store down, bad config...)."""
    return AuthorizationDecision(
        request_id=request_id or "unknown", decision="deny", reason_code=code,  # type: ignore[arg-type]
        message="Internal policy error; request denied (fail closed).", evidence={}, stop_run=False, latency_ms=0)
