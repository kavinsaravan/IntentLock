"""Impact Preview-facing API tests.

SIMULATED DATA: impact reports and patch artifacts here are built from fixtures by the test, NOT produced by a
real Impact Preview sandbox (Person 2 has not implemented one). The promoter is a FAKE (records calls) except in the
HTTP-client tests, which talk to a real local HTTP server. They prove the control plane's enforcement logic.
"""
import hashlib
import json
import pathlib
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import jsonschema
import pytest
from fastapi.testclient import TestClient

from intentlock_api.app import create_app
from intentlock_api.artifacts import LocalDirArtifactReader
from intentlock_api.promoter import PromoterClient, PromoterError, PromoterRequest, PromoterResponse, PromoterReview
from test_api import FIX, H, TOKEN, activate, draft, start_run, authz, req

PATCH = ("diff --git a/src/auth/session.ts b/src/auth/session.ts\n--- a/src/auth/session.ts\n+++ b/src/auth/session.ts\n"
         "@@ -1,2 +1,2 @@\n const a = 1;\n-const timeout = 5;\n+const timeout = 30;\n")
SHA = hashlib.sha256(PATCH.encode()).hexdigest()
ALI, BOB = "reviewer-ali", "reviewer-bob"


class FakePromoter:
    def __init__(self, result=None, exc=None):
        self.calls, self.result, self.exc = [], result, exc

    def promote(self, r: PromoterRequest) -> PromoterResponse:
        self.calls.append(r)
        if self.exc:
            raise self.exc
        return self.result or PromoterResponse(status="promoted", base_commit=r.base_commit, applied_patch_sha256=r.patch_sha256)


class Env:
    """One control plane with a run, an artifact dir and (optionally) a fake promoter."""
    def __init__(self, store, tmp_path, *, promoter="fake", reviewers=None):
        self.store, self.tmp = store, tmp_path
        self.iid = "impact_" + uuid.uuid4().hex[:10]     # unique per test: persistent (real) tables must not collide
        self.promoter = FakePromoter() if promoter == "fake" else promoter
        self.reader = LocalDirArtifactReader(str(tmp_path / "artifacts"))
        self.client = TestClient(create_app(store, internal_token=TOKEN, artifacts=self.reader,
                                            promoter=self.promoter, reviewers=reviewers,
                                            reviewer_tokens={ALI: "ali-test-token", BOB: "bob-test-token"}))
        self.contract = activate(self.client)
        self.run = start_run(self.client, self.contract)

    def report(self, decision="pass", iid=None, write_patch=True, patch=PATCH, **over):
        iid = iid or self.iid
        sha = hashlib.sha256(patch.encode()).hexdigest()
        d = json.loads((FIX / "impact-report.json").read_text())
        key = f"runs/{self.run['run_id']}/patches/{sha}.patch"
        d.update(impact_report_id=iid, run_id=self.run["run_id"], contract_id=self.contract["contract_id"], contract_version=1,
                 base_commit=self.run["base_commit"], patch_s3_key=key, patch_sha256=sha, decision=decision)
        if decision != "block":
            d["tests"][0].update(status="passed", exit_code=0); d["findings"] = []
        d.update(over)
        if write_patch:
            p = self.tmp / "artifacts" / key; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(patch)
        return d

    def ingest(self, report, headers=H):
        return self.client.post("/internal/impact", json=report, headers=headers)

    def body(self, report, who=ALI, **over):
        return {"reviewer": who, "patch_sha256": report["patch_sha256"], "base_commit": report["base_commit"], **over}

    def approve(self, report, who=ALI, **over):
        return self.client.post(f"/api/impact/{report['impact_report_id']}/approve", json=self.body(report, who, **over),
                                headers={"X-IntentLock-Reviewer-Token": "ali-test-token" if who == ALI else "bob-test-token"})

    def promote(self, report, who=ALI, sha=None):
        return self.client.post(f"/api/impact/{report['impact_report_id']}/promote",
                                json={"requested_by": who, "patch_sha256": sha or report["patch_sha256"]},
                                headers={"X-IntentLock-Reviewer-Token": "ali-test-token" if who == ALI else "bob-test-token"})

    def status(self, report):
        return self.client.get(f"/api/impact/{report['impact_report_id']}/status").json()


@pytest.fixture
def env(store, tmp_path):
    return Env(store, tmp_path)


# ---------------------------------------------------------------- ingestion
def test_ingest_requires_token_and_validates_schema(env):
    r = env.report()
    assert env.ingest(r, {}).status_code == 401
    assert env.ingest({**r, "surprise": 1}).status_code == 422
    assert env.ingest({**r, "decision": "maybe"}).status_code == 422
    assert env.ingest({**r, "run_id": "run_nope"}).status_code == 404
    assert env.ingest({**r, "contract_version": 2}).status_code == 409
    assert env.ingest({**r, "base_commit": "f" * 40}).json()["detail"] == "STALE_BASE_COMMIT"
    assert env.client.get(f"/api/impact/{env.iid}").status_code == 404


def test_ingest_is_immutable_idempotent_and_never_promotes(env):
    r = env.report("pass")
    assert env.ingest(r).status_code == 201
    assert env.ingest(r).status_code == 200 and env.ingest(r).json()["stored"] is False       # idempotent
    changed = {**r, "decision": "block"}
    assert env.ingest(changed).json()["detail"] == "IMPACT_EXISTS"                           # cannot be rewritten
    assert env.client.get(f"/api/impact/{env.iid}").json()["decision"] == "pass"
    ev = [e for e in env.client.get(f"/api/runs/{env.run['run_id']}/events").json()["events"] if e["event_type"] == "impact_report"]
    assert len(ev) == 1 and ev[0]["details"]["impact_report_id"] == env.iid and ev[0]["details"]["decision"] == "pass"
    assert env.promoter.calls == []                         # a passing report never triggers promotion


# -------------------------------------------------------------------- reads
def test_patch_endpoint_serves_exact_verified_bytes(env):
    r = env.report(); env.ingest(r)
    resp = env.client.get(f"/api/impact/{env.iid}/patch")
    assert resp.status_code == 200 and resp.text == PATCH and resp.headers["x-patch-sha256"] == SHA
    assert hashlib.sha256(resp.content).hexdigest() == r["patch_sha256"]


def test_tampered_or_missing_patch_is_never_served(env):
    r = env.report(); env.ingest(r)
    path = env.tmp / "artifacts" / r["patch_s3_key"]
    path.write_text(PATCH + "+extra\n")
    resp = env.client.get(f"/api/impact/{env.iid}/patch")
    assert resp.status_code == 409 and resp.json()["detail"] == "PATCH_HASH_MISMATCH" and "extra" not in resp.text
    path.unlink()
    assert env.client.get(f"/api/impact/{env.iid}/patch").status_code == 404


def test_artifact_key_traversal_is_not_readable(env):
    secret = env.tmp / "secret.txt"; secret.write_text("TOPSECRET")
    r = env.report(write_patch=False, patch_s3_key="../secret.txt", patch_sha256=hashlib.sha256(b"TOPSECRET").hexdigest())
    assert env.ingest(r).status_code == 201
    resp = env.client.get(f"/api/impact/{env.iid}/patch")
    assert resp.status_code == 404 and "TOPSECRET" not in resp.text


# ---------------------------------------------------------------- approvals
def test_blocked_patch_is_never_approvable(env):
    r = env.report("block"); env.ingest(r)
    resp = env.approve(r)
    assert resp.status_code == 409 and resp.json()["detail"] == "IMPACT_BLOCKED"
    st = env.status(r)
    assert st["can_approve"] is False and "IMPACT_BLOCKED" in st["approve_blockers"] and st["review"] is None
    assert env.promote(r).status_code == 409 and env.promoter.calls == []
    assert env.store.get_final_review(env.iid) is None


def test_approve_binds_reviewer_contract_base_commit_and_digest(env):
    r = env.report("review_required"); env.ingest(r)
    resp = env.approve(r, comment="looks right")
    assert resp.status_code == 200 and resp.json()["created"] is True
    review = resp.json()["review"]
    jsonschema.validate(review, json.loads((FIX.parent / "schemas/review-approval.schema.json").read_text()))   # shared schema 0.1.0
    assert (review["reviewer"], review["decision"], review["contract_id"], review["contract_version"]) == (ALI, "approve", env.contract["contract_id"], 1)
    assert review["base_commit"] == r["base_commit"] and review["patch_sha256"] == r["patch_sha256"] and review["run_id"] == env.run["run_id"]
    assert env.status(r)["review"]["review_id"] == review["review_id"]
    ev = [e for e in env.client.get(f"/api/runs/{env.run['run_id']}/events").json()["events"] if e["event_type"] == "review_decision"]
    assert ev[0]["details"]["patch_sha256"] == r["patch_sha256"] and ev[0]["details"]["reviewer"] == ALI


def test_approve_rejects_wrong_digest_or_base_commit(env):
    r = env.report(); env.ingest(r)
    assert env.approve(r, patch_sha256="0" * 64).json()["detail"] == "PATCH_HASH_MISMATCH"
    assert env.approve(r, base_commit="e" * 40).json()["detail"] == "STALE_BASE_COMMIT"
    assert env.client.post(f"/api/impact/{env.iid}/approve", json={"reviewer": ALI}).status_code == 422
    assert env.store.get_final_review(env.iid) is None


def test_review_authorization_is_enforced_by_backend(store, tmp_path):
    e = Env(store, tmp_path, reviewers={ALI})
    r = e.report(); e.ingest(r)
    assert e.approve(r, BOB).status_code == 403 and e.approve(r, BOB).json()["detail"] == "REVIEWER_NOT_AUTHORIZED"
    assert e.client.post(f"/api/impact/{e.iid}/reject", json=e.body(r, BOB), headers={"X-IntentLock-Reviewer-Token": "bob-test-token"}).status_code == 403
    assert e.store.get_final_review(e.iid) is None
    assert e.approve(r, ALI).status_code == 200
    assert e.status(r)["reviewer_policy"] == "authenticated allowlist"


def test_one_decision_per_report(env):
    r = env.report(); env.ingest(r)
    assert env.approve(r).json()["created"] is True
    again = env.approve(r)
    assert again.status_code == 200 and again.json()["created"] is False                   # idempotent for the same reviewer
    assert env.approve(r, BOB).json()["detail"] == "ALREADY_REVIEWED"                       # a different reviewer cannot re-decide
    rej = env.client.post(f"/api/impact/{env.iid}/reject", json=env.body(r), headers={"X-IntentLock-Reviewer-Token": "ali-test-token"})
    assert rej.status_code == 409 and rej.json()["detail"] == "ALREADY_REVIEWED"          # approve cannot be flipped to reject


def test_reject_allowed_even_for_blocked_and_makes_promotion_impossible(env):
    r = env.report("block"); env.ingest(r)
    resp = env.client.post(f"/api/impact/{env.iid}/reject", json=env.body(r), headers={"X-IntentLock-Reviewer-Token": "ali-test-token"})
    assert resp.status_code == 200 and resp.json()["review"]["decision"] == "reject"
    assert env.promote(r).json()["detail"] == "NOT_APPROVED" and env.promoter.calls == []


def test_approval_blocked_when_contract_superseded_run_blocked_or_patch_bad(env):
    r = env.report(); env.ingest(r)
    # patch altered after ingestion
    path = env.tmp / "artifacts" / r["patch_s3_key"]; good = path.read_text(); path.write_text("x")
    assert env.approve(r).json()["detail"] == "PATCH_HASH_MISMATCH"
    path.unlink(); assert env.approve(r).json()["detail"] == "PATCH_UNAVAILABLE"
    path.write_text(good)
    # newer contract version supersedes the old one
    activate(env.client, contract_id=env.contract["contract_id"])
    assert env.approve(r).json()["detail"] == "CONTRACT_NOT_ACTIVE"
    assert "CONTRACT_NOT_ACTIVE" in env.status(r)["approve_blockers"] and env.status(r)["can_approve"] is False


def test_approval_blocked_for_security_blocked_run(env):
    r = env.report(); env.ingest(r)
    d = authz(env.client, req(env.run, env.contract, "read_file", {"path": ".env"})).json()
    assert d["stop_run"] is True
    assert env.approve(r).json()["detail"] == "RUN_NOT_ELIGIBLE"


# ---------------------------------------------------------------- promotion
def test_promotion_requires_an_approval(env):
    r = env.report(); env.ingest(r)
    assert env.promote(r).json()["detail"] == "NOT_APPROVED"
    assert env.promoter.calls == [] and env.store.get_promotion(env.iid) is None


def test_pass_report_never_promotes_until_explicitly_requested(env):
    r = env.report("pass"); env.ingest(r)
    env.approve(r)
    assert env.promoter.calls == []                         # approval alone does not promote
    resp = env.promote(r)
    assert resp.status_code == 200 and resp.json()["promotion"]["status"] == "promoted"
    assert len(env.promoter.calls) == 1


def test_promoter_receives_exact_bound_request_and_outcome_is_recorded(env):
    r = env.report(); env.ingest(r); env.approve(r)
    p = env.promote(r).json()["promotion"]
    call = env.promoter.calls[0]
    assert (call.base_commit, call.patch_sha256, call.patch_s3_key) == (r["base_commit"], r["patch_sha256"], r["patch_s3_key"])
    assert (call.run_id, call.contract_id, call.contract_version) == (env.run["run_id"], env.contract["contract_id"], 1)
    assert call.review.review_id == f"rev_{env.iid}" and call.review.reviewer == ALI
    assert p["status"] == "promoted" and p["applied_patch_sha256"] == r["patch_sha256"] and p["completed_at"] and p["requested_by"] == ALI
    assert env.status(r)["promotion"]["status"] == "promoted"
    ev = [e for e in env.client.get(f"/api/runs/{env.run['run_id']}/events").json()["events"] if e["event_type"] == "promotion"]
    assert len(ev) == 1 and ev[0]["details"]["status"] == "promoted" and ev[0]["details"]["patch_sha256"] == r["patch_sha256"]
    assert env.promote(r).json()["detail"] == "ALREADY_REQUESTED" and len(env.promoter.calls) == 1   # at most once


def test_promotion_digest_in_body_must_match_approval(env):
    r = env.report(); env.ingest(r); env.approve(r)
    assert env.promote(r, sha="1" * 64).json()["detail"] == "PATCH_HASH_MISMATCH"
    assert env.promoter.calls == []


def test_promotion_refused_when_patch_tampered_after_approval(env):
    r = env.report(); env.ingest(r); env.approve(r)
    (env.tmp / "artifacts" / r["patch_s3_key"]).write_text(PATCH + "+evil\n")
    assert env.promote(r).json()["detail"] == "PATCH_HASH_MISMATCH"
    assert env.promoter.calls == [] and env.store.get_promotion(env.iid) is None


def test_promotion_refused_when_contract_superseded_after_approval(env):
    r = env.report(); env.ingest(r); env.approve(r)
    activate(env.client, contract_id=env.contract["contract_id"])
    assert env.promote(r).json()["detail"] == "CONTRACT_NOT_ACTIVE" and env.promoter.calls == []


def test_promoter_refusal_is_recorded_not_hidden(store, tmp_path):
    e = Env(store, tmp_path, promoter=FakePromoter(PromoterResponse(status="refused", reason_code="STALE_BASE_COMMIT", message="repo moved")))
    r = e.report(); e.ingest(r); e.approve(r)
    p = e.promote(r).json()["promotion"]
    assert (p["status"], p["reason_code"]) == ("refused", "STALE_BASE_COMMIT") and "applied_patch_sha256" not in p


def test_promoter_outage_fails_closed_and_is_recorded(store, tmp_path):
    e = Env(store, tmp_path, promoter=FakePromoter(exc=PromoterError("URLError")))
    r = e.report(); e.ingest(r); e.approve(r)
    p = e.promote(r).json()["promotion"]
    assert (p["status"], p["reason_code"]) == ("failed", "PROMOTER_UNAVAILABLE")


def test_promoter_claiming_a_different_patch_is_not_reported_as_success(store, tmp_path):
    e = Env(store, tmp_path, promoter=FakePromoter(PromoterResponse(status="promoted", base_commit="a" * 40, applied_patch_sha256="b" * 64)))
    r = e.report(); e.ingest(r); e.approve(r)
    p = e.promote(r).json()["promotion"]
    assert (p["status"], p["reason_code"]) == ("failed", "PATCH_HASH_MISMATCH")


def test_no_promoter_configured_returns_503_and_creates_no_record(store, tmp_path):
    e = Env(store, tmp_path, promoter=None)
    r = e.report(); e.ingest(r); e.approve(r)
    assert e.promote(r).status_code == 503 and e.promote(r).json()["detail"] == "PROMOTER_NOT_CONFIGURED"
    assert e.store.get_promotion(e.iid) is None and e.status(r)["promoter_configured"] is False


def test_status_reports_can_flags_and_blockers(env):
    r = env.report(); env.ingest(r)
    st = env.status(r)
    assert st["can_approve"] is True and st["approve_blockers"] == [] and st["can_promote"] is False and "NOT_APPROVED" in st["promote_blockers"]
    env.approve(r)
    st = env.status(r)
    assert st["can_approve"] is False and st["approve_blockers"] == ["ALREADY_REVIEWED"] and st["can_promote"] is True


def test_stored_items_never_contain_patch_text(env, store):
    r = env.report(); env.ingest(r); env.approve(r); env.promote(r)
    dump = json.dumps(env.client.get(f"/api/runs/{env.run['run_id']}/events").json())
    assert "timeout = 30" not in dump and "const timeout" not in dump
    if hasattr(store, "_t"):
        items = []
        for pk in (f"RUN#{env.run['run_id']}", f"IMPACT#{env.iid}", f"REVIEW#{env.iid}", f"PROMO#{env.iid}"):
            items += store._t.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": pk}, ConsistentRead=True)["Items"]
        assert "const timeout" not in json.dumps(items, default=str)


# ------------------------------------------------- real HTTP promoter client
class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    mode = "ok"

    def do_POST(self):
        n = int(self.headers.get("content-length", 0)); body = json.loads(self.rfile.read(n))
        _Handler.seen.append((self.path, self.headers.get("X-IntentLock-Promoter-Token"), body))
        if _Handler.mode == "redirect":
            self.send_response(307); self.send_header("Location", "http://127.0.0.1:1/x"); self.end_headers(); return
        out = (b'{"status":"promoted","base_commit":"%s","applied_patch_sha256":"%s"}' % (body["base_commit"].encode(), body["patch_sha256"].encode())
               if _Handler.mode == "ok" else b'{"unexpected": true}')
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a): pass


@pytest.fixture
def promoter_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler); _Handler.seen, _Handler.mode = [], "ok"
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _req():
    return PromoterRequest(impact_report_id="i", run_id="r", contract_id="c", contract_version=1, base_commit="a" * 40,
                           patch_sha256="b" * 64, patch_s3_key="k", review=PromoterReview(review_id="rv", reviewer="u", reviewed_at="t", decision="approve"))


def test_http_promoter_client_round_trip(promoter_server):
    res = PromoterClient(promoter_server, "ptoken").promote(_req())
    assert res.status == "promoted" and res.applied_patch_sha256 == "b" * 64
    path, token, body = _Handler.seen[0]
    assert path == "/promote" and token == "ptoken" and body["patch_sha256"] == "b" * 64 and body["review"]["decision"] == "approve"


def test_http_promoter_client_rejects_redirect_and_malformed_and_remote_http(promoter_server):
    _Handler.mode = "redirect"
    with pytest.raises(PromoterError):
        PromoterClient(promoter_server, "t").promote(_req())
    _Handler.mode = "garbage"
    with pytest.raises(PromoterError):
        PromoterClient(promoter_server, "t").promote(_req())
    with pytest.raises(ValueError):
        PromoterClient("http://example.com", "t")
    assert PromoterClient.from_env({}) is None


def test_end_to_end_with_real_http_promoter(store, tmp_path, promoter_server):
    e = Env(store, tmp_path, promoter=PromoterClient(promoter_server, "ptoken"))
    r = e.report(); e.ingest(r); e.approve(r)
    assert e.promote(r).json()["promotion"]["status"] == "promoted"
    assert len(_Handler.seen) == 1


def test_review_requires_token_bound_to_reviewer(env):
    r = env.report(); env.ingest(r)
    path = f"/api/impact/{r['impact_report_id']}/approve"
    assert env.client.post(path, json=env.body(r)).status_code == 401
    assert env.client.post(path, json=env.body(r), headers={"X-IntentLock-Reviewer-Token": "bob-test-token"}).status_code == 401
    assert env.store.get_final_review(r['impact_report_id']) is None
    assert env.approve(r).status_code == 200
    promote = f"/api/impact/{r['impact_report_id']}/promote"
    body = {"requested_by": ALI, "patch_sha256": r['patch_sha256']}
    assert env.client.post(promote, json=body).status_code == 401
    assert env.client.post(promote, json=body, headers={"X-IntentLock-Reviewer-Token": "bob-test-token"}).status_code == 401
    assert env.promoter.calls == []


def test_stop_cancels_approval_and_promotion(env):
    r = env.report(); env.ingest(r)
    env.client.post(f"/api/runs/{env.run['run_id']}/stop", json={})
    assert env.status(r)['can_approve'] is False
    assert 'RUN_NOT_ELIGIBLE' in env.status(r)['approve_blockers']
    assert env.approve(r).json()['detail'] == 'RUN_NOT_ELIGIBLE'
    assert env.promoter.calls == []


def test_stop_after_approval_cancels_promotion(env):
    r = env.report(); env.ingest(r)
    assert env.approve(r).status_code == 200
    env.client.post(f"/api/runs/{env.run['run_id']}/stop", json={})
    assert env.status(r)['can_promote'] is False
    assert 'RUN_NOT_ELIGIBLE' in env.status(r)['promote_blockers']
    assert env.promote(r).json()['detail'] == 'RUN_NOT_ELIGIBLE'
    assert env.promoter.calls == []


def test_unconfigured_reviewer_tokens_fail_closed(store, tmp_path):
    e = Env(store, tmp_path)
    e.client = TestClient(create_app(store, internal_token=TOKEN, artifacts=e.reader, promoter=e.promoter, reviewer_tokens={}))
    r = e.report(); e.ingest(r)
    assert 'REVIEW_AUTH_NOT_CONFIGURED' in e.status(r)['approve_blockers']
    assert e.approve(r).status_code == 401
