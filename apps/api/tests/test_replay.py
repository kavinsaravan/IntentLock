"""TwinGuard Security Replay API.

Control-plane records here are produced by the REAL control plane (policy engine, impact ingestion, review,
promotion state machine) and exported with the REAL execution-plane exporter (export_audit.py). Impact reports
and patches are fixtures (no Docker sandbox ran), and the promoter is a fake, exactly as in test_impact_api.py.
S3 is a fake in-process client: no real AWS is used.
"""
import base64
import hashlib
import importlib.util
import json
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from intentlock_api.app import create_app
from intentlock_api.promoter import PromoterResponse
from intentlock_api.replay import LocalEvidence, S3Evidence, make_redactor, sanitize_patch, sources_from_env
from intentlock_api.store import LocalMemoryStore
from test_api import TOKEN, H, req, authz
from test_impact_api import ALI, PATCH, Env, FakePromoter

ROOT = pathlib.Path(__file__).resolve().parents[3]
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"          # split so repository secret scanners do not flag the fixture


def _exporter():
    spec = importlib.util.spec_from_file_location("replay_test_export", ROOT / "packages/sandbox-provider/export_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApiClient:
    """export_audit.Client stand-in over the TestClient (same GET paths the real exporter uses)."""
    def __init__(self, client):
        self.client = client

    def run(self, run_id):
        return self.client.get(f"/api/runs/{run_id}").json()

    def request(self, path, body=None):
        return self.client.get(path).json()


@pytest.fixture(autouse=True)
def _no_ambient_sources(monkeypatch):
    for k in ("INTENTLOCK_ARTIFACT_DIR", "INTENTLOCK_REPLAY_BUCKET", "INTENTLOCK_PROMOTER_TOKEN"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def env(tmp_path):
    return Env(LocalMemoryStore(), tmp_path)


def propose(env, rid, patch=PATCH):
    r = authz(env.client, req(env.run, env.contract, "propose_patch", {"patch": patch}, rid=rid))
    assert r.status_code == 200
    return r.json()


def finish(env, status="completed"):
    r = env.client.post(f"/internal/runs/{env.run['run_id']}/state", json={"status": status}, headers=H)
    assert r.status_code == 200, r.text


def export(env):
    mod = _exporter()
    key, digest = mod.export(ApiClient(env.client), mod.artifacts.LocalArtifacts(str(env.tmp / "artifacts")),
                             env.run["run_id"])
    return key, digest


def historical(env, **kw):
    """A brand-new API process with an EMPTY store: replay must come from persisted evidence only."""
    return TestClient(create_app(LocalMemoryStore(), internal_token=TOKEN, artifacts=None, promoter=None,
                                 reviewer_tokens={}, replay_sources=[LocalEvidence(str(env.tmp / "artifacts"))], **kw))


def blocked_report(env, iid="impact_blocked01"):
    rep = env.report("block", iid=iid)
    rep["findings"] = [{"severity": "critical", "code": "CRITICAL_TEST_FAILURE",
                        "message": "Approved authentication tests failed", "evidence": "auth-tests"}]
    rep["tests"][0].update(status="failed", exit_code=1)
    return rep


def write_test_output(env, rep, text):
    key = rep["tests"][0]["output_s3_key"] = f"runs/{env.run['run_id']}/tests/auth-tests_{rep['impact_report_id']}.txt"
    p = env.tmp / "artifacts" / key
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ------------------------------------------------------------------------------- full historical replays
def test_promoted_run_replays_from_persisted_audit_after_restart(env):
    env.client.post("/internal/authorize", json=req(env.run, env.contract, "read_file", {"path": "src/auth/session.ts"}, rid="r-read"), headers=H)
    assert propose(env, "r-patch")["decision"] == "allow"
    rep = env.report("pass")
    write_test_output(env, rep, "TAP version 13\nok 1 - timeout is measured in seconds\n")
    assert env.ingest(rep).status_code == 201
    assert env.approve(rep).status_code == 200
    assert env.promote(rep).json()["promotion"]["status"] == "promoted"
    finish(env)
    key, digest = export(env)

    old = env.client.get(f"/api/runs/{env.run['run_id']}")
    assert old.status_code == 200
    hist = historical(env)
    assert hist.get(f"/api/runs/{env.run['run_id']}").status_code == 404      # the live record is gone

    cat = hist.get("/api/replay/runs").json()
    assert [r["run_id"] for r in cat["runs"]] == [env.run["run_id"]]
    assert cat["runs"][0]["evidence"] == "audit_export" and cat["runs"][0]["outcome"] == "safe_patch_promoted"

    v = hist.get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["evidence"]["kind"] == "audit_export" and v["evidence"]["audit"] == {
        "key": key, "sha256": digest, "bytes": (env.tmp / "artifacts" / key).stat().st_size, "source": "local artifact directory"}
    assert v["outcome"]["code"] == "safe_patch_promoted"
    (a,) = v["attempts"]
    assert a["authorization"]["status"] == "allowed" and a["authorization"]["request_id"] == "r-patch"
    assert a["consequence"]["status"] == "passed"
    assert a["review"]["status"] == "approved" and a["review"]["reviewer"] == ALI and a["review"]["matches_patch"]
    assert a["promotion"]["status"] == "promoted" and a["promotion"]["applied_matches_approved"] is True
    assert a["patch"]["status"] == "verified" and a["patch"]["sha256"] == rep["patch_sha256"]
    assert a["patch"]["text"] == PATCH and a["patch"]["files"][0]["path"] == "src/auth/session.ts"
    assert a["tests"][0]["status"] == "passed" and a["tests"][0]["output_status"] == "available"
    assert "ok 1" in a["tests"][0]["output"]
    assert any("identical to the approved patch" in c["claim"] for c in v["claims"])


def test_timeline_is_in_recorded_order_and_contains_only_recorded_stages(env):
    authz(env.client, req(env.run, env.contract, "read_file", {"path": "src/auth/session.ts"}, rid="r1"))
    propose(env, "r2")
    rep = blocked_report(env)
    env.ingest(rep)
    finish(env)
    export(env)
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    stages = [(t["stage"], t["status"]) for t in v["timeline"]]
    assert stages == [("mission_approved", "approved"), ("run_started", "recorded"), ("tool_request", "allowed"),
                      ("tool_request", "allowed"), ("twinguard", "blocked"), ("run_final", "completed")]
    seqs = [t["seq"] for t in v["timeline"] if t["seq"] is not None]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))
    assert not any(t["stage"] in ("review", "promotion") for t in v["timeline"])     # nothing fabricated
    assert v["timeline"][4]["tests"] == [{"test_id": "auth-tests", "status": "failed", "exit_code": 1}]


def test_authorized_patch_can_fail_consequence_check_and_is_never_promotable(env):
    assert propose(env, "r-patch")["decision"] == "allow"
    rep = blocked_report(env)
    write_test_output(env, rep, "not ok 1 - timeout is measured in seconds\n")
    env.ingest(rep)
    assert env.approve(rep).status_code == 409                       # backend refuses; replay must agree
    finish(env)
    export(env)
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    (a,) = v["attempts"]
    assert a["authorization"]["status"] == "allowed"                 # Check 1 passed
    assert a["consequence"]["status"] == "blocked"                   # Check 2 failed
    assert a["consequence"]["failing_tests"] == ["auth-tests"]
    assert a["consequence"]["blocking_findings"] == ["CRITICAL_TEST_FAILURE"]
    assert a["eligibility"]["promotable"] is False and a["eligibility"]["can_promote"] is False
    assert "IMPACT_BLOCKED" in a["eligibility"]["approve_blockers"] and "IMPACT_BLOCKED" in a["eligibility"]["promote_blockers"]
    assert a["review"]["status"] == "none" and a["promotion"]["status"] == "none"
    assert v["outcome"]["code"] == "unsafe_patch_blocked"
    assert any("No promotion was requested for blocked report" in c["claim"] for c in v["claims"])


def test_denied_protected_read_blocks_run_and_replays_as_denied(env):
    authz(env.client, req(env.run, env.contract, "read_file", {"path": "src/auth/session.ts"}, rid="r-ok"))
    d = authz(env.client, req(env.run, env.contract, "read_file", {"path": ".env"}, rid="r-env")).json()
    assert d["decision"] == "deny" and d["reason_code"] == "PROTECTED_PATH"
    export(env)                                                       # run is blocked => terminal
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["run"]["status"] == "blocked" and v["outcome"]["code"] == "unauthorized_request_denied"
    auth = v["authorization"]
    assert (auth["allowed"], auth["denied"]) == (1, 1)
    assert auth["denied_requests"][0]["reason_code"] == "PROTECTED_PATH"
    assert auth["denied_requests"][0]["arguments"] == {"path": ".env"}
    tools = [t for t in v["timeline"] if t["stage"] == "tool_request"]
    assert [(t["request_id"], t["status"]) for t in tools] == [("r-ok", "allowed"), ("r-env", "denied")]
    assert v["timeline"][-1]["stage"] == "run_final" and v["timeline"][-1]["status"] == "blocked"
    assert v["attempts"] == []


def test_approval_alone_is_not_shown_as_promotion(env):
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    env.approve(rep)
    v = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()     # live (not exported yet)
    (a,) = v["attempts"]
    assert a["review"]["status"] == "approved" and a["promotion"]["status"] == "none"
    assert v["outcome"]["code"] == "approved_not_promoted"


def test_refused_promotion_is_not_shown_as_promoted(tmp_path):
    env = Env(LocalMemoryStore(), tmp_path, promoter=FakePromoter(PromoterResponse(status="refused", reason_code="BASE_COMMIT_MISMATCH")))
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    env.approve(rep)
    env.promote(rep)
    v = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["attempts"][0]["promotion"]["status"] == "refused"
    assert v["outcome"]["code"] == "promotion_failed" and "BASE_COMMIT_MISMATCH" in v["outcome"]["detail"]
    assert not any("identical to the approved patch" in c["claim"] for c in v["claims"])


def test_forged_promotion_success_without_matching_digest_is_flagged(env):
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    env.approve(rep)
    env.promote(rep)
    finish(env)
    key, _ = export(env)
    path = env.tmp / "artifacts" / key
    data = json.loads(path.read_text())
    data["impacts"][0]["status"]["promotion"]["applied_patch_sha256"] = "f" * 64
    path.chmod(0o600)
    path.write_text(json.dumps(data))
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["attempts"][0]["promotion"]["status"] == "inconsistent"
    assert v["outcome"]["code"] == "promotion_failed"


@pytest.mark.parametrize("field,value", [
    ("patch_sha256", "f" * 64), ("base_commit", "f" * 40), ("review_id", "rev_other")])
def test_promotion_requires_matching_approval_record(env, field, value):
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    env.approve(rep)
    env.promote(rep)
    finish(env)
    key, _ = export(env)
    path = env.tmp / "artifacts" / key
    data = json.loads(path.read_text())
    if field == "review_id":
        data["impacts"][0]["status"]["promotion"][field] = value
    else:
        data["impacts"][0]["status"]["review"][field] = value
    path.chmod(0o600)
    path.write_text(json.dumps(data))
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["attempts"][0]["promotion"]["status"] == "inconsistent"
    assert v["outcome"]["code"] == "promotion_failed"
    assert not any("identical to the approved patch" in c["claim"] for c in v["claims"])


def test_patch_authorization_requires_matching_recorded_digest(env):
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    env.approve(rep)
    env.promote(rep)
    finish(env)
    key, _ = export(env)
    path = env.tmp / "artifacts" / key
    data = json.loads(path.read_text())
    patch_event = next(e for e in data["events"] if e.get("tool") == "propose_patch")
    patch_event["arguments_redacted"]["patch_sha256"] = "f" * 64
    path.chmod(0o600)
    path.write_text(json.dumps(data))
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["attempts"][0]["authorization"]["status"] == "not_recorded"


# --------------------------------------------------------------------------------------- missing evidence
def test_twinguard_only_evidence_reports_missing_control_plane_records(env):
    rep = blocked_report(env, "impact_only01")
    rep["tests"] = []
    impact = env.tmp / "artifacts" / f"runs/{env.run['run_id']}/impact/impact_only01.json"
    impact.parent.mkdir(parents=True, exist_ok=True)
    impact.write_text(json.dumps(rep))
    hist = historical(env)
    (summary,) = hist.get("/api/replay/runs").json()["runs"]
    assert summary["evidence"] == "twinguard_artifacts" and summary["status"] is None
    v = hist.get(f"/api/replay/runs/{env.run['run_id']}").json()
    (a,) = v["attempts"]
    assert a["authorization"]["status"] == "unavailable"
    assert a["review"]["status"] == "unavailable" and a["promotion"]["status"] == "unavailable"
    assert a["eligibility"]["promotable"] is False
    assert a["consequence"]["tests_recorded"] == 0 and a["tests"] == []          # missing, not "passed"
    assert v["authorization"]["available"] is False
    assert v["run"] is None and v["contract"] is None
    assert any("No final audit export exists" in x for x in v["limitations"])
    assert [t["stage"] for t in v["timeline"]] == ["twinguard"]


def test_attempts_follow_creation_time_and_outcome_names_its_attempt(env):
    first = blocked_report(env, "impact_zzz_first")          # ids sort opposite to creation order
    first["created_at"] = "2026-11-07T10:00:00Z"
    second = env.report("pass", iid="impact_aaa_second", created_at="2026-11-07T10:05:00Z")
    for rep in (first, second):
        p = env.tmp / "artifacts" / f"runs/{env.run['run_id']}/impact/{rep['impact_report_id']}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep))
    v = historical(env).get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert [a["impact_report_id"] for a in v["attempts"]] == ["impact_zzz_first", "impact_aaa_second"]
    assert v["outcome"]["code"] == "unsafe_patch_blocked" and v["outcome"]["impact_report_id"] == "impact_zzz_first"
    assert "later attempt (impact_aaa_second) passed TwinGuard; its review evidence is unavailable" in v["outcome"]["detail"]


def test_missing_patch_and_test_output_are_reported_not_hidden(env):
    propose(env, "r-patch")
    rep = env.report("pass", write_patch=False)
    rep["tests"][0]["output_s3_key"] = f"runs/{env.run['run_id']}/tests/never_written.txt"
    env.ingest(rep)
    v = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()
    a = v["attempts"][0]
    assert a["patch"]["status"] == "unavailable" and a["patch"]["text"] is None
    assert a["tests"][0]["status"] == "passed" and a["tests"][0]["output_status"] == "unavailable"


def test_tampered_patch_bytes_are_never_displayed(env):
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    (env.tmp / "artifacts" / rep["patch_s3_key"]).write_text(PATCH + "+evil\n")
    a = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()["attempts"][0]
    assert a["patch"]["status"] == "hash_mismatch" and a["patch"]["text"] is None
    assert not any("hashes to the reported" in c["claim"] for c in env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()["claims"])


def test_malformed_audit_is_rejected_and_reported(env):
    finish(env, "failed")
    key, _ = export(env)
    path = env.tmp / "artifacts" / key
    path.chmod(0o600)
    path.write_text('{"run": {"run_id": "someone_else"}}')
    hist = historical(env)
    assert hist.get(f"/api/replay/runs/{env.run['run_id']}").status_code == 404
    path.write_text("not json")
    assert hist.get(f"/api/replay/runs/{env.run['run_id']}").status_code == 404


def test_live_run_without_export_is_labelled_as_unpersisted(env):
    authz(env.client, req(env.run, env.contract, "read_file", {"path": "src/auth/session.ts"}, rid="r1"))
    v = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()
    assert v["evidence"]["kind"] == "live_store" and v["evidence"]["audit"] is None
    assert v["outcome"]["code"] == "run_active"
    assert any("not persisted" in x for x in v["limitations"])
    assert env.client.get(f"/api/replay/runs/{env.run['run_id']}?source=local").status_code == 404


# ------------------------------------------------------------------------------- input and access control
@pytest.mark.parametrize("bad", ["..", "run..x", "run$1", "a" * 101, "run%2F..", "runs%2Frun_x%2Faudit"])
def test_invalid_run_ids_are_rejected(env, bad):
    r = env.client.get(f"/api/replay/runs/{bad}")
    assert r.status_code in (400, 404) and r.status_code != 200
    if r.status_code == 400:
        assert r.json()["detail"] == "INVALID_RUN_ID"


def test_unknown_run_and_invalid_source(env):
    assert env.client.get("/api/replay/runs/run_doesnotexist").json()["detail"] == "RUN_EVIDENCE_NOT_FOUND"
    assert env.client.get(f"/api/replay/runs/{env.run['run_id']}?source=file").status_code == 422
    assert env.client.get("/api/replay/runs/run_x?key=runs/run_x/audit/final.json").status_code == 404


def test_artifact_keys_outside_the_run_are_never_read(env, tmp_path):
    other = "runs/run_other/patches/" + hashlib.sha256(PATCH.encode()).hexdigest() + ".patch"
    (env.tmp / "artifacts" / other).parent.mkdir(parents=True, exist_ok=True)
    (env.tmp / "artifacts" / other).write_text(PATCH)
    propose(env, "r-patch")
    rep = env.report("pass", write_patch=False, patch_s3_key=other)
    rep["tests"][0]["output_s3_key"] = "runs/run_other/tests/stolen.txt"
    env.ingest(rep)
    a = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()["attempts"][0]
    assert a["patch"]["status"] == "key_invalid" and a["patch"]["text"] is None
    assert a["tests"][0]["output_status"] == "key_invalid" and a["tests"][0]["output"] is None


def test_local_source_ignores_symlinks_and_foreign_files(tmp_path):
    root = tmp_path / "art"
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (root / "runs" / "run_a" / "audit").mkdir(parents=True)
    (root / "runs" / "run_a" / "audit" / "final.json").symlink_to(secret)
    (root / "runs" / "run_a" / "notes.txt").write_text("x")
    (root / "runs" / "run_link").symlink_to(tmp_path)
    src = LocalEvidence(str(root))
    assert src.list_keys() == {"run_a": []}
    with pytest.raises(Exception):
        src.read("runs/run_a/audit/final.json")
    with pytest.raises(Exception):
        src.read("runs/run_a/../../secret.txt")


# ----------------------------------------------------------------------------------- redaction and diffs
def test_secrets_tokens_and_protected_hunks_are_redacted(tmp_path):
    env = Env(LocalMemoryStore(), tmp_path)
    patch = (PATCH + "diff --git a/.env.local b/.env.local\n--- a/.env.local\n+++ b/.env.local\n@@ -1 +1 @@\n"
             "-DB_PASSWORD=old\n+DB_PASSWORD=hunter2\n"
             "diff --git a/src/auth/keys.ts b/src/auth/keys.ts\n--- a/src/auth/keys.ts\n+++ b/src/auth/keys.ts\n"
             f"@@ -1 +1 @@\n-const k = '';\n+const k = '{AWS_KEY}'; // {TOKEN}\n")
    rep = env.report("block", patch=patch)
    env.ingest(rep)
    body = env.client.get(f"/api/replay/runs/{env.run['run_id']}").text
    assert "hunter2" not in body and "DB_PASSWORD" not in body
    assert AWS_KEY not in body and TOKEN not in body
    assert "ali-test-token" not in body and "bob-test-token" not in body
    p = json.loads(body)["attempts"][0]["patch"]
    assert p["status"] == "verified" and p["withheld_files"] == [".env.local"] and p["redactions"] == 2
    assert [f["path"] for f in p["files"]] == ["src/auth/session.ts", ".env.local", "src/auth/keys.ts"]
    assert [f["withheld"] for f in p["files"]] == [False, True, False]


def test_sanitize_patch_handles_large_malformed_and_non_git_diffs():
    redact = make_redactor([])
    junk = sanitize_patch("<script>alert(1)</script>\nnot a diff", [".env*"], redact)
    assert junk["parsed"] is False and junk["text"].startswith("<script>")   # returned as data; never rendered as HTML
    plain = sanitize_patch("--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-A=1\n+A=2\n", [".env*"], redact)
    assert plain["text"] == "" and plain["withheld_files"] == [".env"]
    renamed = sanitize_patch("diff --git a/src/a.ts b/infra/a.ts\nrename from src/a.ts\nrename to infra/a.ts\n@@ -1 +1 @@\n-x\n+y\n",
                             ["infra/**"], redact)
    assert renamed["withheld_files"] == ["infra/a.ts"] and "+y" not in renamed["text"]


def test_large_and_non_utf8_patches_are_bounded(env):
    propose(env, "r-patch")
    big = PATCH + "".join(f"+line {i} " + "x" * 80 + "\n" for i in range(3000))
    rep = env.report("pass", patch=big)
    env.ingest(rep)
    a = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()["attempts"][0]
    assert a["patch"]["status"] == "verified" and a["patch"]["truncated"] is True
    assert a["patch"]["bytes"] == len(big.encode()) and len(a["patch"]["text"]) <= 200_000
    raw = PATCH.encode() + b"+\xff\xfe broken\n"
    sha = hashlib.sha256(raw).hexdigest()
    key = f"runs/{env.run['run_id']}/patches/{sha}.patch"
    (env.tmp / "artifacts" / key).write_bytes(raw)
    rep2 = env.report("pass", iid="impact_bin01", write_patch=False, patch_s3_key=key, patch_sha256=sha)
    env.ingest(rep2)
    a2 = env.client.get(f"/api/replay/runs/{env.run['run_id']}").json()["attempts"][1]
    assert a2["patch"]["status"] == "verified" and "�" in a2["patch"]["text"]


# ---------------------------------------------------------------------------------------- S3 (fake client)
class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]

    def close(self):
        pass


class FakeS3:
    def __init__(self, objects, tamper=()):
        self.objects, self.tamper, self.calls = objects, set(tamper), []

    def list_objects_v2(self, **kw):
        self.calls.append(("list", kw))
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        start = int(kw.get("ContinuationToken", 0))
        page = keys[start:start + 2]
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more,
                **({"NextContinuationToken": str(start + 2)} if more else {})}

    def get_object(self, **kw):
        self.calls.append(("get", kw["Key"]))
        data = self.objects[kw["Key"]]
        digest = hashlib.sha256(data).hexdigest()
        meta = "0" * 64 if kw["Key"] in self.tamper else digest
        return {"Body": FakeBody(data), "ContentLength": len(data), "Metadata": {"sha256": meta},
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode()}

    def put_object(self, **kw):
        raise AssertionError("replay must never write to S3")


def test_s3_source_lists_reads_and_verifies(env):
    propose(env, "r-patch")
    rep = env.report("pass")
    env.ingest(rep)
    env.approve(rep)
    env.promote(rep)
    finish(env)
    key, _ = export(env)
    art = env.tmp / "artifacts"
    objects = {str(p.relative_to(art)): p.read_bytes() for p in art.rglob("*") if p.is_file()}
    objects["runs/s3_smoke/tests/roundtrip.txt"] = b"x"
    objects["runs/run_x/input/repository.tar.gz"] = b"x"
    fake = FakeS3(objects)
    src = S3Evidence("intentlock-evidence-test", profile=None, region="us-east-1", client=fake)
    hist = TestClient(create_app(LocalMemoryStore(), internal_token=TOKEN, artifacts=None, promoter=None,
                                 reviewer_tokens={}, replay_sources=[src]))
    cat = hist.get("/api/replay/runs").json()
    assert [r["run_id"] for r in cat["runs"]] == [env.run["run_id"]] and cat["sources"][0]["id"] == "s3"
    v = hist.get(f"/api/replay/runs/{env.run['run_id']}?source=s3").json()
    assert v["evidence"]["source"] == "s3" and v["outcome"]["code"] == "safe_patch_promoted"
    assert v["attempts"][0]["patch"]["status"] == "verified"
    assert all(c[0] in ("list", "get") for c in fake.calls)
    assert not any(c[0] == "get" and "input/" in c[1] for c in fake.calls)

    fake.tamper.add(rep["patch_s3_key"])
    hist2 = TestClient(create_app(LocalMemoryStore(), internal_token=TOKEN, artifacts=None, promoter=None, reviewer_tokens={},
                                  replay_sources=[S3Evidence("intentlock-evidence-test", profile=None, region="us-east-1", client=fake)]))
    p = hist2.get(f"/api/replay/runs/{env.run['run_id']}").json()["attempts"][0]["patch"]
    assert p["status"] == "hash_mismatch" and p["text"] is None


def test_sources_from_env_never_uses_default_profile(tmp_path):
    srcs, notes = sources_from_env({"INTENTLOCK_REPLAY_BUCKET": "intentlock-evidence-test"})
    assert srcs == [] and notes[0]["available"] is False
    srcs, notes = sources_from_env({"INTENTLOCK_REPLAY_BUCKET": "intentlock-evidence-test", "AWS_PROFILE": "default"})
    assert srcs == [] and "default profile" in notes[0]["reason"]
    srcs, notes = sources_from_env({"INTENTLOCK_ARTIFACT_DIR": str(tmp_path)})
    assert [s.id for s in srcs] == ["local"] and notes == []
    assert sources_from_env({}) == ([], [])


def test_catalog_reports_unconfigured_and_existing_routes_unchanged(env):
    cat = env.client.get("/api/replay/runs").json()
    assert cat["runs"] == [] and cat["sources"] == [] and cat["live_lookup"]["persistent"] is False
    rep = env.report("pass")
    env.ingest(rep)
    st = env.client.get(f"/api/impact/{rep['impact_report_id']}/status").json()
    assert set(st) == {"impact_report_id", "decision", "review", "promotion", "can_approve", "approve_blockers",
                       "can_promote", "promote_blockers", "reviewer_policy", "promoter_configured", "artifact_reader"}
    assert env.client.get(f"/api/impact/{rep['impact_report_id']}/patch").text == PATCH
