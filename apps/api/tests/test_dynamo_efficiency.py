"""DynamoDB round-trip budget and correctness of the latency optimisations (MOCKED AWS via moto)."""
import collections
import json
import pathlib
import threading
import time

import boto3
import botocore.handlers
import pytest

from intentlock_api import service
from intentlock_api.dynamo_store import TABLE_DEFINITION, DynamoStore
from intentlock_api.store import now
from intentlock_policy.models import MissionContract, Run

FIX = pathlib.Path(__file__).resolve().parents[3] / "packages" / "contracts" / "fixtures"
MAX_CALLS_PER_AUTHORIZATION = 6      # was 9 before the optimisation; every call is ~75 ms from a laptop to us-east-1


@pytest.fixture
def counted(moto_env):
    """A moto-backed DynamoStore whose every DynamoDB API call is counted."""
    from moto import mock_aws
    calls = collections.Counter()
    lock = threading.Lock()

    def hook(model, **_):
        with lock:
            calls[model.name] += 1

    entry = ("before-call.dynamodb", hook)
    botocore.handlers.BUILTIN_HANDLERS.append(entry)     # applies to clients created after this point
    try:
        with mock_aws():
            boto3.client("dynamodb", region_name="us-east-1").create_table(TableName="eff-test", **TABLE_DEFINITION)
            yield DynamoStore("eff-test", region="us-east-1"), calls
    finally:
        botocore.handlers.BUILTIN_HANDLERS.remove(entry)


def seed(store, max_calls=1000):
    d = json.loads((FIX / "contract.json").read_text())
    d.update(contract_id=f"c_eff_{time.time_ns()}", version=1); d["limits"]["max_tool_calls"] = max_calls
    c = MissionContract.model_validate(d); store.put_contract(c); t = now()
    r = Run(run_id=f"run_eff_{time.time_ns()}", contract_id=c.contract_id, contract_version=1, status="active",
            base_commit=c.base_commit, created_at=t, started_at=t); store.put_run(r)
    return c, r


def req(c, r, rid="q"):
    return {"request_id": rid, "run_id": r.run_id, "contract_id": c.contract_id, "contract_version": 1, "tool": "read_file",
            "arguments": {"path": "src/auth/session.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"}


def test_authorization_stays_within_the_dynamodb_call_budget(counted):
    store, calls = counted
    c, r = seed(store)
    service.authorize_request(req(c, r, "warm"), store)          # first call also seeds the event-seq hint
    per = []
    for i in range(5):
        before = sum(calls.values())
        d = service.authorize_request(req(c, r, f"q{i}"), store)
        assert d.decision == "allow"
        per.append(sum(calls.values()) - before)
    assert max(per) <= MAX_CALLS_PER_AUTHORIZATION, per
    assert [e.seq for e in store.list_events(r.run_id)] == list(range(1, 7))     # still exactly one gap-free event each


def test_stale_seq_hint_still_yields_gap_free_events(counted):
    """Two writers (two DynamoStore objects = two hints) interleave appends on one run: the conditional write
    detects the stale hint, re-reads the counter and retries, so sequence numbers stay exact."""
    store_a, _ = counted
    store_b = DynamoStore("eff-test", region="us-east-1")
    c, r = seed(store_a)
    for i in range(10):
        (store_a if i % 2 == 0 else store_b).append_event(r.run_id, event_type="agent_event", status="recorded", message=str(i))
    assert [e.seq for e in store_a.list_events(r.run_id)] == list(range(1, 11))


def test_update_event_with_base_matches_update_without(counted):
    store, calls = counted
    c, r = seed(store)
    e1 = store.append_event(r.run_id, event_type="tool_request", status="pending", tool="read_file")
    e2 = store.append_event(r.run_id, event_type="tool_request", status="pending", tool="read_file")
    before = sum(calls.values())
    a = store.update_event(r.run_id, e1.seq, base=e1, status="decided", decision="allow", reason_code="ALLOW")
    used_with_base = sum(calls.values()) - before
    before = sum(calls.values())
    b = store.update_event(r.run_id, e2.seq, status="decided", decision="allow", reason_code="ALLOW")
    used_without = sum(calls.values()) - before
    assert used_with_base == 1 and used_without == 2                # base skips the read round trip
    assert (a.decision, a.reason_code, a.status) == (b.decision, b.reason_code, b.status)
    assert store.list_events(r.run_id)[0].decision == "allow"


def test_shared_client_is_thread_safe_under_concurrent_authorizations(counted):
    store, _ = counted
    c, r = seed(store, max_calls=5)
    out, errs = [], []

    def w():
        try: out.append(service.authorize_request(req(c, r), store))
        except Exception as e: errs.append(repr(e)[:150])

    ts = [threading.Thread(target=w) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs, errs
    assert sum(o.decision == "allow" for o in out) == 5 and sum(o.reason_code == "TOOL_CALL_LIMIT_EXCEEDED" for o in out) == 7


def test_warm_up_never_raises(counted):
    store, _ = counted
    store.warm_up()
    bad = DynamoStore("does-not-exist", region="us-east-1")
    bad.warm_up()                                                     # missing table: swallowed, not fatal


def test_transaction_conflicts_on_plain_updates_are_retried_not_surfaced(counted):
    """Real DynamoDB rejects plain writes to the run item during an in-flight event-append transaction.
    moto never does that, so inject it: two conflicts, then success."""
    from botocore.exceptions import ClientError
    store, _ = counted
    c, r = seed(store)
    real = store._t.update_item
    state = {"left": 2}

    def flaky(**kw):
        if state["left"] > 0:
            state["left"] -= 1
            raise ClientError({"Error": {"Code": "TransactionConflictException", "Message": "x"}}, "UpdateItem")
        return real(**kw)

    store._t.update_item = flaky
    assert store.reserve_tool_call(r.run_id) == 0 and state["left"] == 0         # conflicts absorbed, count exact
    state["left"] = 2
    assert store.transition_run(r.run_id, ["active"], status="stopped", stop_reason="x").status == "stopped"


def test_non_conflict_errors_are_not_retried_forever(counted):
    from botocore.exceptions import ClientError
    store, _ = counted
    c, r = seed(store)
    n = {"c": 0}

    def boom(**kw):
        n["c"] += 1
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "UpdateItem")

    store._t.update_item = boom
    from intentlock_api.store import StoreError
    with pytest.raises(StoreError):
        store.reserve_tool_call(r.run_id)
    assert n["c"] == 1                                                            # fails closed immediately
