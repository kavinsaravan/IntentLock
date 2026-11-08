import json
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from intentlock_api.app import create_app
from intentlock_api.store import LocalMemoryStore

FIX = pathlib.Path(__file__).resolve().parents[3] / "packages" / "contracts" / "fixtures"
TOKEN = "test-internal-token"
H = {"X-IntentLock-Internal-Token": TOKEN}
SHA = "0123456789abcdef0123456789abcdef01234567"


def draft():
    return json.loads((FIX / "contract-draft.json").read_text())


def fake_invoker(outputs):
    it = iter(outputs)
    return lambda system, user: next(it)


@pytest.fixture
def client(store):  # runs every test below against memory AND MOCKED DynamoDB (and real, if configured)
    return TestClient(create_app(store, internal_token=TOKEN))


def activate(client, d=None, **kw):
    r = client.post("/api/contracts", json={"draft": d or draft(), "approved": True, "approved_by": "tester", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def start_run(client, c):
    r = client.post("/api/runs", json={"contract_id": c["contract_id"], "contract_version": c["version"]})
    assert r.status_code == 201, r.text
    return r.json()


def req(run, c, tool, args, rid="r1"):
    return {"request_id": rid, "run_id": run["run_id"], "contract_id": c["contract_id"],
            "contract_version": c["version"], "tool": tool, "arguments": args,
            "agent_id": "a", "timestamp": "2026-11-07T10:01:00Z"}


def authz(client, body, headers=H):
    return client.post("/internal/authorize", json=body, headers=headers)


# ---- contracts: human approval + bedrock safety ------------------------
def test_approval_required(client):
    r = client.post("/api/contracts", json={"draft": draft(), "approved": False, "approved_by": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "APPROVAL_REQUIRED"
    r = client.post("/api/contracts", json={"draft": draft(), "approved_by": "x"})
    assert r.status_code == 422


def test_invalid_draft_cannot_be_activated(client):
    bad = draft(); bad["network_access"] = True
    assert client.post("/api/contracts", json={"draft": bad, "approved": True, "approved_by": "x"}).status_code == 422
    bad = draft(); bad["allowed_paths"] = ["../../etc"]
    assert client.post("/api/contracts", json={"draft": bad, "approved": True, "approved_by": "x"}).status_code == 422


def test_propose_invalid_json_cannot_become_contract():
    c = TestClient(create_app(LocalMemoryStore(), invoker=fake_invoker(["not json", "{still bad"]), internal_token=TOKEN))
    r = c.post("/api/contracts/propose", json={"task": "fix it", "base_commit": SHA})
    assert r.status_code == 502 and "CONTRACT_PROPOSAL_FAILED" in r.json()["detail"]
    assert c.get("/api/contracts/x/versions/1").status_code == 404


def test_propose_retries_once_and_returns_draft_not_contract():
    good = json.dumps({k: v for k, v in draft().items() if k not in ("repository_id", "base_commit")})
    c = TestClient(create_app(LocalMemoryStore(), invoker=fake_invoker(["oops", "```json\n" + good + "\n```"]), internal_token=TOKEN))
    r = c.post("/api/contracts/propose", json={"task": "fix it", "base_commit": SHA, "repository_id": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["attempts"] == 2 and body["requires_human_approval"] is True
    assert "contract_id" not in body["draft"] and body["draft"]["repository_id"] == "demo"


def test_propose_cannot_enable_network_or_override_commit():
    obj = {k: v for k, v in draft().items()}
    obj["network_access"] = True; obj["base_commit"] = "f" * 40
    c = TestClient(create_app(LocalMemoryStore(), invoker=fake_invoker([json.dumps(obj)]), internal_token=TOKEN))
    d = c.post("/api/contracts/propose", json={"task": "t", "base_commit": SHA}).json()["draft"]
    assert d["network_access"] is False and d["base_commit"] == SHA


def test_new_version_supersedes_old(client):
    c1 = activate(client)
    c2 = activate(client, contract_id=c1["contract_id"])
    assert c2["version"] == 2
    assert client.get(f"/api/contracts/{c1['contract_id']}/versions/1").json()["status"] == "superseded"
    assert client.get(f"/api/contracts/{c1['contract_id']}/versions/2").json()["status"] == "active"
    run = start_run(client, c2)
    # a run bound to v1 can no longer be created
    assert client.post("/api/runs", json={"contract_id": c1["contract_id"], "contract_version": 1}).status_code == 409
    assert authz(client, req(run, c2, "read_file", {"path": "src/auth/a.ts"})).json()["decision"] == "allow"


# ---- internal auth -----------------------------------------------------
def test_internal_endpoints_require_token(client):
    c = activate(client); run = start_run(client, c)
    b = req(run, c, "read_file", {"path": "src/auth/a.ts"})
    assert authz(client, b, {}).status_code == 401
    assert authz(client, b, {"X-IntentLock-Internal-Token": "wrong"}).status_code == 401
    assert client.post("/internal/events", json={"run_id": run["run_id"], "event_type": "agent_event"}).status_code == 401


def test_internal_api_fails_closed_without_configured_token():
    c = TestClient(create_app(LocalMemoryStore(), internal_token=""))
    assert c.post("/internal/authorize", json={}, headers=H).status_code == 503


# ---- scenario A: allow, deny, stop, replay -----------------------------
def test_scenario_a_deny_env_blocks_run_and_replays_denied(client):
    c = activate(client); run = start_run(client, c)
    ok = authz(client, req(run, c, "read_file", {"path": "src/auth/session.ts"}, "r1")).json()
    assert (ok["decision"], ok["reason_code"]) == ("allow", "ALLOW")
    ok2 = authz(client, req(run, c, "search_code", {"query": "timeout", "paths": ["src/auth/**"]}, "r2")).json()
    assert ok2["decision"] == "allow"
    bad = authz(client, req(run, c, "read_file", {"path": ".env"}, "r3")).json()
    assert (bad["decision"], bad["reason_code"], bad["stop_run"]) == ("deny", "PROTECTED_PATH", True)
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "blocked"
    after = authz(client, req(run, c, "read_file", {"path": "src/auth/session.ts"}, "r4")).json()
    assert (after["decision"], after["reason_code"]) == ("deny", "RUN_NOT_ACTIVE")
    evs = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    tool_evs = [e for e in evs if e["event_type"] == "tool_request"]
    assert [(e["request_id"], e["decision"], e["reason_code"]) for e in tool_evs] == [
        ("r1", "allow", "ALLOW"), ("r2", "allow", "ALLOW"),
        ("r3", "deny", "PROTECTED_PATH"), ("r4", "deny", "RUN_NOT_ACTIVE")]
    assert all(e["status"] == "decided" and e["latency_ms"] is not None for e in tool_evs)
    assert any(e["event_type"] == "run_stopped" for e in evs)
    seqs = [e["seq"] for e in evs]; assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))


def test_stop_run_commits_state_then_denies(client):
    c = activate(client); run = start_run(client, c)
    r = client.post(f"/api/runs/{run['run_id']}/stop", json={"reason": "user clicked stop"})
    body = r.json()
    assert r.status_code == 200 and body["state_committed"] is True and body["run"]["status"] == "stopped"
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "stopped"  # state is durable at response time
    d = authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"})).json()
    assert d["reason_code"] == "RUN_NOT_ACTIVE"
    assert client.post(f"/api/runs/{run['run_id']}/stop").json()["run"]["status"] == "stopped"  # idempotent


def test_unknown_run_and_malformed_bodies_fail_closed(client):
    c = activate(client); run = start_run(client, c)
    ghost = req(run, c, "read_file", {"path": "src/auth/a.ts"}); ghost["run_id"] = "run_nope"
    assert authz(client, ghost).json()["reason_code"] == "RUN_NOT_ACTIVE"
    for bad in ({}, [], "x", {"request_id": "q"}):
        d = authz(client, bad)
        assert d.status_code == 200 and d.json()["decision"] == "deny"


def test_tool_call_limit_via_api(client):
    dr = draft(); dr["limits"]["max_tool_calls"] = 2
    c = activate(client, dr); run = start_run(client, c)
    codes = [authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"}, f"r{i}")).json()["reason_code"] for i in range(3)]
    assert codes == ["ALLOW", "ALLOW", "TOOL_CALL_LIMIT_EXCEEDED"]


def test_events_never_store_patch_contents_or_secrets(client):
    c = activate(client); run = start_run(client, c)
    secret = "TOPSECRETPATCHBODY"
    patch = f"diff --git a/src/auth/a.ts b/src/auth/a.ts\n--- a/src/auth/a.ts\n+++ b/src/auth/a.ts\n@@ -1 +1 @@\n-a\n+{secret}\n"
    assert authz(client, req(run, c, "propose_patch", {"patch": patch})).json()["decision"] == "allow"
    authz(client, req(run, c, "search_code", {"query": "AKIAABCDEFGHIJKLMNOP", "paths": ["src/auth/**"]}, "r2"))
    client.post("/internal/events", headers=H, json={"run_id": run["run_id"], "event_type": "agent_event",
                "details": {"patch": secret, "token": "abc", "note": "ok"}})
    dump = json.dumps(client.get(f"/api/runs/{run['run_id']}/events").json())
    assert secret not in dump and "AKIAABCDEFGHIJKLMNOP" not in dump
    assert "patch_sha256" in dump


def test_audit_log_has_no_contents(client, caplog):
    c = activate(client); run = start_run(client, c)
    with caplog.at_level("INFO", logger="intentlock.audit"):
        authz(client, req(run, c, "read_file", {"path": ".env"}))
    assert "PROTECTED_PATH" in caplog.text and "arguments" not in caplog.text


# ---- ingestion, agent info, streaming ---------------------------------
def test_event_ingestion_and_agent_info(client):
    c = activate(client); run = start_run(client, c)
    r = client.post(f"/internal/runs/{run['run_id']}/agent", headers=H, json={"agent_process_id": "pid-42", "sandbox_id": "sb1"})
    assert r.json()["agent_process_id"] == "pid-42"
    r = client.post("/internal/events", headers=H, json={"run_id": run["run_id"], "event_type": "impact_report", "message": "blocked"})
    assert r.status_code == 201
    assert client.post("/internal/events", headers=H, json={"run_id": run["run_id"], "event_type": "tool_request"}).status_code == 422
    assert client.post("/internal/events", headers=H, json={"run_id": "nope", "event_type": "agent_event"}).status_code == 404


def test_sse_stream_delivers_real_events(client):
    c = activate(client); run = start_run(client, c)
    authz(client, req(run, c, "read_file", {"path": ".env"}))
    with client.stream("GET", f"/api/runs/{run['run_id']}/events/stream?timeout_seconds=0.5") as r:
        text = "".join(r.iter_text())
    assert "event: tool_request" in text and "PROTECTED_PATH" in text and "event: run_stopped" in text


def test_latency_is_measured_and_small(client):
    c = activate(client); run = start_run(client, c)
    d = authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"})).json()
    assert 0 <= d["latency_ms"] < 250


# ---- expiry marks the run inactive BEFORE denying -----------------------
def test_expired_run_is_marked_inactive_before_denial(client, monkeypatch):
    import datetime
    import intentlock_api.app as appmod
    dr = draft(); dr["limits"]["run_timeout_seconds"] = 60
    c = activate(client, dr); run = start_run(client, c)
    real = appmod.now
    monkeypatch.setattr(appmod, "now", lambda: real() + datetime.timedelta(seconds=61))
    # even a request that would also fail argument validation reports the inactive run
    d = authz(client, req(run, c, "read_file", {"path": "../x"})).json()
    assert (d["decision"], d["reason_code"]) == ("deny", "RUN_NOT_ACTIVE")
    assert d["evidence"]["run_status"] == "stopped"
    got = client.get(f"/api/runs/{run['run_id']}").json()
    assert got["status"] == "stopped" and got["stop_reason"] == "run_timeout" and got["stopped_at"]
    evs = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    assert [e["event_type"] for e in evs].count("run_stopped") == 1


def test_expiry_visible_on_plain_get(client, monkeypatch):
    import datetime
    import intentlock_api.app as appmod
    dr = draft(); dr["limits"]["run_timeout_seconds"] = 60
    c = activate(client, dr); run = start_run(client, c)
    real = appmod.now
    monkeypatch.setattr(appmod, "now", lambda: real() + datetime.timedelta(seconds=61))
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "stopped"


def test_sse_resumes_from_last_event_id_header(client):
    c = activate(client); run = start_run(client, c)
    authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"}, "ra"))
    authz(client, req(run, c, "read_file", {"path": "src/auth/b.ts"}, "rb"))
    with client.stream("GET", f"/api/runs/{run['run_id']}/events/stream?timeout_seconds=0.5",
                       headers={"Last-Event-ID": "2"}) as r:
        text = "".join(r.iter_text())
    assert "\"request_id\":\"rb\"" in text and "\"request_id\":\"ra\"" not in text
    assert "event: run_started" not in text


def test_request_permission_and_finish_task_are_conditional(client):
    dr = draft(); dr["limits"]["max_tool_calls"] = 2
    c = activate(client, dr); run = start_run(client, c)
    ok = authz(client, req(run, c, "finish_task", {"summary": "x"}, "f1")).json()
    assert ok["decision"] == "allow"
    bad = authz(client, req(run, c, "request_permission", {"tool": "t"}, "p1")).json()
    assert bad["reason_code"] == "INVALID_ARGUMENTS"          # argument validation applies
    lim = authz(client, req(run, c, "finish_task", {"summary": "x"}, "f2")).json()
    assert lim["reason_code"] == "TOOL_CALL_LIMIT_EXCEEDED"   # limits apply (denied calls count)
    client.post(f"/api/runs/{run['run_id']}/stop")
    assert authz(client, req(run, c, "finish_task", {"summary": "x"}, "f3")).json()["reason_code"] == "RUN_NOT_ACTIVE"


# ---- DynamoDB-era additions (run on every store) -------------------------
def test_internal_state_transition_completed_and_failed(client):
    c = activate(client); run = start_run(client, c)
    r = client.post(f"/internal/runs/{run['run_id']}/state", headers=H, json={"status": "completed", "reason": "agent finished"})
    assert r.status_code == 200 and r.json()["transitioned"] is True and r.json()["run"]["status"] == "completed"
    assert r.json()["run"]["completed_at"]
    again = client.post(f"/internal/runs/{run['run_id']}/state", headers=H, json={"status": "failed"}).json()
    assert again["transitioned"] is False and again["run"]["status"] == "completed"   # first writer wins
    d = authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"})).json()
    assert d["reason_code"] == "RUN_NOT_ACTIVE"
    evs = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    assert [e["event_type"] for e in evs].count("run_completed") == 1
    assert client.post(f"/internal/runs/{run['run_id']}/state", json={"status": "completed"}).status_code == 401
    assert client.post(f"/internal/runs/{run['run_id']}/state", headers=H, json={"status": "active"}).status_code == 422


def test_stop_then_complete_does_not_overwrite_stop(client):
    c = activate(client); run = start_run(client, c)
    client.post(f"/api/runs/{run['run_id']}/stop")
    r = client.post(f"/internal/runs/{run['run_id']}/state", headers=H, json={"status": "completed"}).json()
    assert r["transitioned"] is False and r["run"]["status"] == "stopped"


def test_event_ids_and_seq_stable_across_reads(client):
    c = activate(client); run = start_run(client, c)
    authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"}))
    a = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    b = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    assert a == b and [e["seq"] for e in a] == [1, 2]
    assert client.get(f"/api/runs/{run['run_id']}/events?after_seq=1").json()["events"][0]["seq"] == 2


def test_tool_limit_is_exact_under_concurrent_requests(store):
    import threading
    from intentlock_api import service
    from intentlock_api.store import now
    from intentlock_policy.models import MissionContract, Run
    d = json.loads((FIX / "contract.json").read_text()); d["limits"]["max_tool_calls"] = 5
    d.update(contract_id="c_conc_" + SHA[:6] + str(time.time_ns()), version=1)
    c = MissionContract.model_validate(d); store.put_contract(c)
    t = now(); r = Run(run_id="run_conc_" + str(time.time_ns()), contract_id=c.contract_id, contract_version=1,
                       status="active", base_commit=c.base_commit, created_at=t, started_at=t)
    store.put_run(r)
    body = {"request_id": "x", "run_id": r.run_id, "contract_id": c.contract_id, "contract_version": 1,
            "tool": "read_file", "arguments": {"path": "src/auth/a.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"}
    out, errs = [], []

    def worker():
        try:
            out.append(service.authorize_request(dict(body), store))
        except Exception as e:                              # surfaced in the assertion message (known intermittent flake)
            errs.append(f"{type(e).__name__}: {str(e)[:200]}")

    ts = [threading.Thread(target=worker) for _ in range(12)]
    [x.start() for x in ts]; [x.join() for x in ts]
    allowed = [o for o in out if o.decision == "allow"]
    limited = [o for o in out if o.reason_code == "TOOL_CALL_LIMIT_EXCEEDED"]
    seqs = [e.seq for e in store.list_events(r.run_id)]
    ctx = f"results={len(out)} allowed={len(allowed)} limited={len(limited)} thread_errors={errs} seqs={seqs}"
    assert not errs, ctx
    assert len(allowed) == 5 and len(limited) == 7, ctx
    assert seqs == list(range(1, 13)), ctx                 # one gap-free event per request


def test_stop_during_evaluation_turns_allow_into_deny(store, monkeypatch):
    from intentlock_api import service
    from intentlock_api.store import now
    from intentlock_policy.models import MissionContract, Run
    d = json.loads((FIX / "contract.json").read_text()); d.update(contract_id="c_toctou_" + str(time.time_ns()), version=1)
    c = MissionContract.model_validate(d); store.put_contract(c)
    t = now(); r = Run(run_id="run_toctou_" + str(time.time_ns()), contract_id=c.contract_id, contract_version=1,
                       status="active", base_commit=c.base_commit, created_at=t, started_at=t)
    store.put_run(r)
    real = service.authorize
    def stop_midway(*a, **k):
        dec = real(*a, **k)
        service.stop_run_state(store, r.run_id, "stopped", "user")   # a stop lands after the policy said ALLOW
        return dec
    monkeypatch.setattr(service, "authorize", stop_midway)
    body = {"request_id": "x", "run_id": r.run_id, "contract_id": c.contract_id, "contract_version": 1,
            "tool": "read_file", "arguments": {"path": "src/auth/a.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"}
    dec = service.authorize_request(body, store)
    assert (dec.decision, dec.reason_code) == ("deny", "RUN_NOT_ACTIVE")
    ev = [e for e in store.list_events(r.run_id) if e.event_type == "tool_request"][0]
    assert ev.decision == "deny"                                       # recorded decision matches what was returned


def test_dynamo_items_contain_no_secrets_or_patch_text(client, store):
    if not hasattr(store, "_t"):
        pytest.skip("memory store has no items to scan")
    c = activate(client); run = start_run(client, c)
    secret = "ZZTOPSECRETPATCHBODYZZ"
    patch = f"diff --git a/src/auth/a.ts b/src/auth/a.ts\n--- a/src/auth/a.ts\n+++ b/src/auth/a.ts\n@@ -1 +1 @@\n-a\n+{secret}\n"
    assert authz(client, req(run, c, "propose_patch", {"patch": patch})).json()["decision"] == "allow"
    authz(client, req(run, c, "read_file", {"path": ".env"}, "r2"))
    items = store._t.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": f"RUN#{run['run_id']}"}, ConsistentRead=True)["Items"]
    dump = json.dumps(items, default=str)
    assert secret not in dump and "TOKEN" not in dump.upper().replace("TOKENS", "")
    assert "patch_sha256" in dump


def test_store_failure_fails_closed_on_authorize(client, store, monkeypatch):
    c = activate(client); run = start_run(client, c)
    def boom(*a, **k): raise RuntimeError("dynamodb down")
    monkeypatch.setattr(store, "reserve_tool_call", boom)
    r = authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"}))
    assert r.status_code == 200 and r.json()["decision"] == "deny" and r.json()["reason_code"] == "POLICY_ENGINE_ERROR"


def test_health_reports_store_backend(client, store):
    assert client.get("/health").json()["store"] == store.backend
