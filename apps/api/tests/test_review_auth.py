"""Adversarial tests for reviewer authentication (merged safeguards branch + hardening).
All data is simulated (fixture reports, fake promoter); AWS is not involved (memory store, fixtures)."""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from intentlock_api import impact
from intentlock_api.app import create_app
from intentlock_api.store import LocalMemoryStore
from test_api import H, TOKEN, authz, req
from test_impact_api import ALI, BOB, Env

TOK = {ALI: "ali-test-token", BOB: "bob-test-token"}
HDR = "X-IntentLock-Reviewer-Token"


@pytest.fixture
def env(tmp_path):
    return Env(LocalMemoryStore(), tmp_path)


def post(env, r, action, who=ALI, token="ali-test-token", **extra):
    h = {} if token is None else {HDR: (token.encode("latin-1") if not token.isascii() else token)}   # raw bytes for non-ASCII
    body = {"requested_by": who, "patch_sha256": r["patch_sha256"]} if action == "promote" else env.body(r, who)
    return env.client.post(f"/api/impact/{r['impact_report_id']}/{action}", json=body, headers=h, **extra)


@pytest.mark.parametrize("action", ["approve", "reject", "promote"])
@pytest.mark.parametrize("token", [None, "", "   ", "wrong", "ali-test-toke", "ali-test-token ", "ALI-TEST-TOKEN", "bob-test-token", "tök-é-ñ", "x" * 5000])
def test_every_review_endpoint_rejects_missing_or_wrong_tokens(env, action, token):
    r = env.report(); env.ingest(r)
    resp = post(env, r, action, ALI, token)
    assert resp.status_code == 401 and resp.json()["detail"] == "REVIEWER_NOT_AUTHENTICATED"   # never a 500, never success
    assert env.store.get_final_review("impact_01") is None and env.promoter.calls == []


def test_token_is_bound_to_the_named_reviewer(env):
    r = env.report(); env.ingest(r)
    assert post(env, r, "approve", BOB, "ali-test-token").status_code == 401        # Bob's name with Ali's token
    assert post(env, r, "approve", "mallory", "ali-test-token").status_code == 401  # unknown reviewer name
    assert post(env, r, "approve", ALI, "ali-test-token").status_code == 200


def test_authentication_precedes_every_other_check(env):
    """A wrong token must not reveal digest/base-commit/blocked state (401 before 409)."""
    r = env.report("block"); env.ingest(r)
    bad = env.client.post("/api/impact/impact_01/approve", json={"reviewer": ALI, "patch_sha256": "0" * 64, "base_commit": "f" * 40}, headers={HDR: "nope"})
    assert bad.status_code == 401
    good = env.client.post("/api/impact/impact_01/approve", json={"reviewer": ALI, "patch_sha256": "0" * 64, "base_commit": "f" * 40}, headers={HDR: "ali-test-token"})
    assert good.status_code == 409


def test_unconfigured_tokens_fail_closed_for_all_actions(tmp_path):
    store = LocalMemoryStore()
    e = Env(store, tmp_path)
    e.client = TestClient(create_app(store, internal_token=TOKEN, artifacts=e.reader, promoter=e.promoter, reviewer_tokens={}))
    r = e.report(iid="impact_unconf"); e.ingest(r)
    for action in ("approve", "reject", "promote"):
        assert post(e, r, action, ALI, "anything-at-all-123456").status_code == 401
    st = e.status(r)
    assert st["can_approve"] is False and "REVIEW_AUTH_NOT_CONFIGURED" in st["approve_blockers"]


def test_env_default_without_tokens_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("INTENTLOCK_REVIEWER_TOKENS", raising=False)
    store = LocalMemoryStore()
    e = Env(store, tmp_path)
    e.client = TestClient(create_app(store, internal_token=TOKEN, artifacts=e.reader, promoter=e.promoter))   # reviewer_tokens="env"
    r = e.report(iid="impact_env"); e.ingest(r)
    assert post(e, r, "approve", ALI, "ali-test-token").status_code == 401


@pytest.mark.parametrize("bad", ['not json', '[]', '"x"', '{"a": 1}', '{"": "long-enough-token-0123456789"}', '{"alice": "short"}',
                                 '{"alice": "replace-with-random-secret"}', '{"alice": "CHANGE-ME-generate-with-openssl-rand-hex-24"}',
                                 '{"alice": "same-long-token-0123456789", "bob": "same-long-token-0123456789"}'])
def test_token_map_parsing_rejects_weak_placeholder_shared_or_malformed(bad):
    with pytest.raises(ValueError):
        impact.reviewer_tokens_from_env({"INTENTLOCK_REVIEWER_TOKENS": bad})


def test_token_map_parsing_accepts_strong_unique_tokens_and_empty():
    ok = {"alice": "a" * 48, "bob": "b" * 48}
    assert impact.reviewer_tokens_from_env({"INTENTLOCK_REVIEWER_TOKENS": json.dumps(ok)}) == ok
    assert impact.reviewer_tokens_from_env({}) == {}


def test_malformed_env_stops_the_app_from_starting(monkeypatch):
    monkeypatch.setenv("INTENTLOCK_REVIEWER_TOKENS", '{"alice": "short"}')
    with pytest.raises(ValueError):
        create_app(LocalMemoryStore(), internal_token=TOKEN, artifacts=None, promoter=None, reviewers=None)


def test_tokens_never_appear_in_responses_status_events_or_logs(env, caplog):
    r = env.report(); env.ingest(r)
    with caplog.at_level(logging.DEBUG):
        outs = [post(env, r, "approve", ALI, "wrong-secret-value-xyz"), post(env, r, "approve"), env.client.get("/api/impact/impact_01/status"),
                env.client.get(f"/api/runs/{env.run['run_id']}/events")]
        post(env, r, "promote")
    dump = " ".join(o.text for o in outs) + caplog.text
    for secret in ("ali-test-token", "bob-test-token", "wrong-secret-value-xyz"):
        assert secret not in dump


@pytest.mark.parametrize("how", ["stopped", "blocked", "failed"])
def test_inactive_runs_cannot_approve_or_promote(env, how):
    r = env.report(); env.ingest(r)
    if how == "stopped":
        env.client.post(f"/api/runs/{env.run['run_id']}/stop", json={})
    elif how == "blocked":
        authz(env.client, req(env.run, env.contract, "read_file", {"path": ".env"}))
    else:
        env.client.post(f"/internal/runs/{env.run['run_id']}/state", headers=H, json={"status": "failed"})
    assert post(env, r, "approve").json()["detail"] == "RUN_NOT_ELIGIBLE"
    assert env.status(r)["can_approve"] is False and env.promoter.calls == []


def test_stop_after_approval_blocks_promotion_even_with_valid_token(env):
    r = env.report(); env.ingest(r)
    assert post(env, r, "approve").status_code == 200
    env.client.post(f"/api/runs/{env.run['run_id']}/stop", json={})
    assert post(env, r, "promote").json()["detail"] == "RUN_NOT_ELIGIBLE" and env.promoter.calls == []


def test_completed_run_can_still_be_reviewed(env):
    r = env.report(); env.ingest(r)
    env.client.post(f"/internal/runs/{env.run['run_id']}/state", headers=H, json={"status": "completed"})
    assert post(env, r, "approve").status_code == 200


def test_exact_patch_verification_still_applies_with_valid_token(env):
    r = env.report(); env.ingest(r)
    assert env.client.post("/api/impact/impact_01/approve", json={**env.body(r), "patch_sha256": "1" * 64}, headers={HDR: "ali-test-token"}).json()["detail"] == "PATCH_HASH_MISMATCH"
    (env.tmp / "artifacts" / r["patch_s3_key"]).write_text("tampered")
    assert post(env, r, "approve").json()["detail"] == "PATCH_HASH_MISMATCH"
    assert env.store.get_final_review("impact_01") is None
