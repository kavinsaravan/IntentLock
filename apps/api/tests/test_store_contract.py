"""One behavioural contract, run against every store (memory, MOCKED DynamoDB, optionally REAL)."""
import json
import pathlib
import threading
import uuid
from datetime import datetime, timezone

import pytest

from intentlock_api.store import ContractExists, RunExists
from intentlock_policy.models import MissionContract, ReviewApproval, Run

FIX = pathlib.Path(__file__).resolve().parents[3] / "packages" / "contracts" / "fixtures"
T0 = datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc)


def uid(p): return f"{p}_{uuid.uuid4().hex[:10]}"


def mk_contract(cid=None, version=1):
    d = json.loads((FIX / "contract.json").read_text())
    d.update(contract_id=cid or uid("c"), version=version)
    return MissionContract.model_validate(d)


def mk_run(c, rid=None, status="active"):
    return Run(run_id=rid or uid("run"), contract_id=c.contract_id, contract_version=c.version, status=status,
               base_commit=c.base_commit, created_at=T0, started_at=T0)


def threads(n, fn):
    out, errs = [], []
    def w(i):
        try: out.append(fn(i))
        except Exception as e: errs.append(e)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs, errs
    return out


def test_contract_versions_are_immutable(store):
    c = mk_contract()
    store.put_contract(c)
    with pytest.raises(ContractExists):
        store.put_contract(c.model_copy(update={"mission": "tampered"}))
    assert store.get_contract(c.contract_id, 1).mission == c.mission
    store.set_contract_status(c.contract_id, 1, "superseded")
    got = store.get_contract(c.contract_id, 1)
    assert got.status == "superseded" and got.mission == c.mission and got.allowed_paths == c.allowed_paths
    assert store.get_contract(c.contract_id, 2) is None and store.get_contract(uid("nope"), 1) is None


def test_latest_version(store):
    c1 = mk_contract(); store.put_contract(c1)
    assert store.latest_version(c1.contract_id) == 1
    store.put_contract(mk_contract(c1.contract_id, 2)); store.put_contract(mk_contract(c1.contract_id, 10))
    assert store.latest_version(c1.contract_id) == 10   # numeric, not lexicographic
    assert store.latest_version(uid("none")) == 0


def test_run_roundtrip_and_duplicate(store):
    r = mk_run(mk_contract())
    store.put_run(r)
    assert store.get_run(r.run_id) == r
    with pytest.raises(RunExists):
        store.put_run(r)
    assert store.get_run(uid("missing")) is None


def test_transition_is_compare_and_set(store):
    r = mk_run(mk_contract()); store.put_run(r)
    stopped = store.transition_run(r.run_id, ["active"], status="stopped", stopped_at=T0, stop_reason="first")
    assert stopped.status == "stopped" and stopped.stop_reason == "first"
    assert store.transition_run(r.run_id, ["active", "created"], status="blocked", stop_reason="second") is None
    cur = store.get_run(r.run_id)
    assert cur.status == "stopped" and cur.stop_reason == "first"       # loser changed nothing
    assert store.transition_run(uid("ghost"), ["active"], status="stopped") is None


def test_transition_race_has_exactly_one_winner(store):
    r = mk_run(mk_contract()); store.put_run(r)
    res = threads(16, lambda i: store.transition_run(r.run_id, ["active"], status="stopped", stop_reason=f"t{i}"))
    winners = [x for x in res if x is not None]
    assert len(winners) == 1
    assert store.get_run(r.run_id).stop_reason == winners[0].stop_reason


def test_update_run_cannot_change_status(store):
    r = mk_run(mk_contract()); store.put_run(r)
    with pytest.raises(ValueError):
        store.update_run(r.run_id, status="completed")
    u = store.update_run(r.run_id, agent_process_id="pid-1", sandbox_id="sb-1")
    assert (u.agent_process_id, u.sandbox_id, u.status) == ("pid-1", "sb-1", "active")


def test_tool_call_reservation_is_exact_under_concurrency(store):
    r = mk_run(mk_contract()); store.put_run(r)
    priors = threads(25, lambda i: store.reserve_tool_call(r.run_id))
    assert sorted(priors) == list(range(25))


def test_event_seq_is_gapless_and_ordered_under_concurrency(store):
    r = mk_run(mk_contract()); store.put_run(r)
    evs = threads(20, lambda i: store.append_event(r.run_id, event_type="agent_event", status="recorded", message=f"m{i}"))
    assert sorted(e.seq for e in evs) == list(range(1, 21))
    listed = store.list_events(r.run_id)
    assert [e.seq for e in listed] == list(range(1, 21))
    assert len({e.event_id for e in listed}) == 20
    assert all(e.event_id == f"evt_{r.run_id}_{e.seq:08d}" for e in listed)   # stable ids for reconnects
    assert [e.seq for e in store.list_events(r.run_id, after_seq=17)] == [18, 19, 20]
    assert store.list_events(r.run_id, after_seq=20) == []


def test_update_event_keeps_identity_and_order(store):
    r = mk_run(mk_contract()); store.put_run(r)
    a = store.append_event(r.run_id, event_type="tool_request", status="pending", tool="read_file")
    store.append_event(r.run_id, event_type="agent_event", status="recorded")
    u = store.update_event(r.run_id, a.seq, status="decided", decision="deny", reason_code="PROTECTED_PATH", latency_ms=0.25)
    assert (u.seq, u.event_id) == (a.seq, a.event_id)
    again = store.list_events(r.run_id)
    assert [e.seq for e in again] == [1, 2] and again[0].decision == "deny" and again[0].latency_ms == 0.25


def test_many_events_stay_ordered(store):
    r = mk_run(mk_contract()); store.put_run(r)
    for _ in range(120):
        store.append_event(r.run_id, event_type="agent_event", status="recorded")
    assert [e.seq for e in store.list_events(r.run_id)] == list(range(1, 121))


def test_events_isolated_per_run(store):
    c = mk_contract(); r1, r2 = mk_run(c), mk_run(c); store.put_run(r1); store.put_run(r2)
    store.append_event(r1.run_id, event_type="agent_event", status="recorded")
    assert store.list_events(r2.run_id) == []
    assert store.append_event(r2.run_id, event_type="agent_event", status="recorded").seq == 1


def test_reviews(store):
    rid = uid("impact")
    mk = lambda n: ReviewApproval(review_id=f"rev_{n}", impact_report_id=rid, run_id="run_x", contract_id="c", contract_version=1,
                                  base_commit="0" * 40, patch_sha256="a" * 64, decision="approve", reviewer="u",
                                  reviewed_at=datetime(2026, 11, 7, 10, 0, n, tzinfo=timezone.utc))
    store.put_review(mk(2)); store.put_review(mk(1))
    assert [r.review_id for r in store.list_reviews(rid)] == ["rev_1", "rev_2"]
    with pytest.raises(ContractExists):
        store.put_review(mk(1))
    assert store.list_reviews(uid("none")) == []
