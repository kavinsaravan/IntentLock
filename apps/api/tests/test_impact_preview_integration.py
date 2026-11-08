"""Cross-workstream integration: exercises the full Impact Preview flow through the control-plane API.

Tests the complete path Person 2's code will follow: authorize MCP requests, submit impact reports,
review/approve, and promote. Uses a REAL local promoter HTTP server (not a mock) and the actual
artifact reader. Does NOT need Docker — the sandbox output is simulated but the entire API,
artifact storage, review, and promotion pipeline is real.

Covers PRD Scenarios A, B, C at the control-plane level.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from intentlock_api.app import create_app
from intentlock_api.artifacts import LocalDirArtifactReader
from intentlock_api.promoter import PromoterClient
from test_api import FIX, TOKEN, H

REVIEWER = "reviewer-demo"
REVIEWER_TOKEN = "integration-reviewer-token-secure"
PROMOTER_TOKEN = "integration-promoter-token-secure"

SAFE_PATCH = (
    "diff --git a/src/auth/session.ts b/src/auth/session.ts\n"
    "--- a/src/auth/session.ts\n"
    "+++ b/src/auth/session.ts\n"
    "@@ -1,5 +1,5 @@\n"
    " export function isSessionExpired(createdAt: number, now: number, timeoutSeconds: number): boolean {\n"
    "-  return now - createdAt >= timeoutSeconds;\n"
    "+  return now - createdAt >= timeoutSeconds * 1000;\n"
    " }\n"
    " \n"
    " export function verifyToken(token: string): boolean {\n"
)
SAFE_SHA = hashlib.sha256(SAFE_PATCH.encode()).hexdigest()

UNSAFE_PATCH = (
    "diff --git a/src/auth/session.ts b/src/auth/session.ts\n"
    "--- a/src/auth/session.ts\n"
    "+++ b/src/auth/session.ts\n"
    "@@ -3,5 +3,5 @@ export function isSessionExpired(createdAt: number, now: number, timeoutSeconds:\n"
    " }\n"
    " \n"
    " export function verifyToken(token: string): boolean {\n"
    "-  return token === 'demo-valid-token';\n"
    "+  return true;\n"
    " }\n"
)
UNSAFE_SHA = hashlib.sha256(UNSAFE_PATCH.encode()).hexdigest()


def _make_promoter_handler(repo_dir, artifact_dir, api_url, internal_token):
    """A real promoter that verifies the patch artifact and applies it to a git repo."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            import hmac as _hmac
            tok = self.headers.get("X-IntentLock-Promoter-Token", "")
            if self.path != "/promote" or not _hmac.compare_digest(tok, PROMOTER_TOKEN):
                self.send_error(403)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 16384:
                self.send_error(400)
                return
            req = json.loads(self.rfile.read(size))
            reply = _real_promote(req, repo_dir, artifact_dir, api_url, internal_token)
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Handler


def _real_promote(req, repo_dir, artifact_dir, api_url, internal_token):
    """Minimal real promoter logic matching Kavin's promoter.promote.py contract."""
    import urllib.request
    try:
        key = req["patch_s3_key"]
        artifact = pathlib.Path(artifact_dir) / key
        if not artifact.is_file():
            return {"status": "refused", "reason_code": "ARTIFACT_NOT_FOUND"}
        patch = artifact.read_bytes()
        if hashlib.sha256(patch).hexdigest() != req["patch_sha256"]:
            return {"status": "refused", "reason_code": "PATCH_HASH_MISMATCH"}
        head = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).decode().strip()
        if head != req["base_commit"]:
            return {"status": "refused", "reason_code": "STALE_BASE_COMMIT"}
        status = subprocess.check_output(["git", "-C", str(repo_dir), "status", "--porcelain"]).decode().strip()
        if status:
            return {"status": "refused", "reason_code": "WORKTREE_NOT_CLEAN"}
        check = subprocess.run(["git", "-C", str(repo_dir), "apply", "--check", "-"], input=patch, capture_output=True)
        if check.returncode:
            return {"status": "refused", "reason_code": "PATCH_NOT_APPLICABLE"}
        headers = {"X-IntentLock-Internal-Token": internal_token}
        r = urllib.request.Request(api_url + "/api/runs/" + req["run_id"], headers=headers)
        with urllib.request.urlopen(r, timeout=3) as resp:
            run = json.loads(resp.read())
        if run.get("status") not in ("active", "completed"):
            return {"status": "refused", "reason_code": "RUN_NOT_ACTIVE"}
        applied = subprocess.run(["git", "-C", str(repo_dir), "apply", "-"], input=patch, capture_output=True)
        if applied.returncode:
            return {"status": "refused", "reason_code": "PATCH_APPLY_FAILED"}
        return {"status": "promoted", "base_commit": req["base_commit"], "applied_patch_sha256": req["patch_sha256"]}
    except Exception:
        return {"status": "refused", "reason_code": "VERIFICATION_UNAVAILABLE"}


class IntegrationEnv:
    """Full integration environment: API, artifact dir, real promoter, git demo repo."""
    def __init__(self, store, tmp_path):
        self.store = store
        self.tmp = tmp_path
        self.artifacts = tmp_path / "artifacts"
        self.artifacts.mkdir()
        self.repo = tmp_path / "demo-repo"
        self.repo.mkdir()
        (self.repo / "src" / "auth").mkdir(parents=True)
        (self.repo / "tests" / "auth").mkdir(parents=True)
        (self.repo / "src" / "auth" / "session.ts").write_text(
            'export function isSessionExpired(createdAt: number, now: number, timeoutSeconds: number): boolean {\n'
            '  return now - createdAt >= timeoutSeconds;\n'
            '}\n\n'
            "export function verifyToken(token: string): boolean {\n"
            "  return token === 'demo-valid-token';\n"
            '}\n'
        )
        (self.repo / "tests" / "auth" / "session.test.ts").write_text("test('placeholder', () => {});\n")
        (self.repo / "package.json").write_text('{"private":true,"scripts":{"test:auth":"echo ok"}}\n')
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@intentlock.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "IntentLock Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "baseline"], check=True)
        self.base_commit = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"]
        ).decode().strip()

        self.reader = LocalDirArtifactReader(str(self.artifacts))
        self.promoter_server = None
        self.promoter_url = None
        self.promoter_client = None

        self.client = TestClient(create_app(
            store, internal_token=TOKEN, artifacts=self.reader,
            promoter=None,
            reviewers=None,
            reviewer_tokens={REVIEWER: REVIEWER_TOKEN},
        ))

    def start_promoter(self):
        """Start the real HTTP promoter server (deferred so we can get the API URL)."""
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        handler = _make_promoter_handler(self.repo, self.artifacts, "http://testserver", TOKEN)
        self.promoter_server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.promoter_url = f"http://127.0.0.1:{port}"
        self.promoter_thread = threading.Thread(target=self.promoter_server.serve_forever, daemon=True)
        self.promoter_thread.start()
        self.promoter_client = PromoterClient(self.promoter_url, PROMOTER_TOKEN)

    def stop_promoter(self):
        if self.promoter_server:
            self.promoter_server.shutdown()
            self.promoter_thread.join(5)
            self.promoter_server.server_close()

    def create_contract(self):
        d = json.loads((FIX / "contract-draft.json").read_text())
        d["base_commit"] = self.base_commit
        d["allowed_paths"] = ["src/auth/**"]
        resp = self.client.post("/api/contracts", json={"draft": d, "approved": True, "approved_by": "tester"})
        assert resp.status_code == 201, resp.text
        return resp.json()

    def create_run(self, contract):
        resp = self.client.post("/api/runs", json={
            "contract_id": contract["contract_id"], "contract_version": contract["version"]})
        assert resp.status_code == 201, resp.text
        return resp.json()

    def authorize(self, run, contract, tool, args, rid=None):
        body = {
            "request_id": rid or f"q_{uuid.uuid4().hex[:8]}",
            "run_id": run["run_id"],
            "contract_id": contract["contract_id"],
            "contract_version": contract["version"],
            "tool": tool,
            "arguments": args,
            "agent_id": "kiro-cli",
            "timestamp": "2026-11-07T10:00:00Z",
        }
        return self.client.post("/internal/authorize", json=body, headers=H).json()

    def submit_impact(self, run, contract, patch_text, decision, test_exit=0, findings=None):
        patch_bytes = patch_text.encode()
        sha = hashlib.sha256(patch_bytes).hexdigest()
        iid = "impact_" + uuid.uuid4().hex[:12]
        key = f"runs/{run['run_id']}/patches/{sha}.patch"
        artifact_path = self.artifacts / key
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(patch_bytes)
        report = {
            "impact_report_id": iid,
            "run_id": run["run_id"],
            "sandbox_id": "sandbox_" + uuid.uuid4().hex[:12],
            "contract_id": contract["contract_id"],
            "contract_version": contract["version"],
            "base_commit": run["base_commit"],
            "patch_s3_key": key,
            "patch_sha256": sha,
            "summary": {"files_modified": 1, "files_created": 0, "files_deleted": 0,
                        "files_renamed": 0, "lines_added": 1, "lines_deleted": 1},
            "tests": [{"test_id": "auth-tests", "status": "passed" if test_exit == 0 else "failed",
                        "exit_code": test_exit, "duration_ms": 500, "output_s3_key": f"runs/{run['run_id']}/tests/auth-tests.txt"}],
            "findings": findings or [],
            "decision": decision,
            "created_at": "2026-11-07T10:05:00Z",
        }
        resp = self.client.post("/internal/impact", json=report, headers=H)
        assert resp.status_code == 201, resp.text
        return report

    def approve(self, report):
        resp = self.client.post(
            f"/api/impact/{report['impact_report_id']}/approve",
            json={"reviewer": REVIEWER, "patch_sha256": report["patch_sha256"],
                  "base_commit": report["base_commit"]},
            headers={"X-IntentLock-Reviewer-Token": REVIEWER_TOKEN})
        return resp

    def promote_via_api(self, report):
        resp = self.client.post(
            f"/api/impact/{report['impact_report_id']}/promote",
            json={"requested_by": REVIEWER, "patch_sha256": report["patch_sha256"]},
            headers={"X-IntentLock-Reviewer-Token": REVIEWER_TOKEN})
        return resp

    def get_status(self, report):
        return self.client.get(f"/api/impact/{report['impact_report_id']}/status").json()


@pytest.fixture
def ienv(store, tmp_path):
    env = IntegrationEnv(store, tmp_path)
    yield env
    env.stop_promoter()


class TestScenarioA:
    """Authorized read succeeds; .env read blocks the run; subsequent requests rejected."""
    def test_read_then_env_block(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        d1 = ienv.authorize(run, contract, "read_file", {"path": "src/auth/session.ts"})
        assert d1["decision"] == "allow" and d1["reason_code"] == "ALLOW"
        d2 = ienv.authorize(run, contract, "read_file", {"path": ".env"})
        assert d2["decision"] == "deny" and d2["reason_code"] == "PROTECTED_PATH"
        assert d2["stop_run"] is True
        d3 = ienv.authorize(run, contract, "read_file", {"path": "src/auth/session.ts"})
        assert d3["decision"] == "deny" and d3["reason_code"] == "RUN_NOT_ACTIVE"
        run_state = ienv.client.get(f"/api/runs/{run['run_id']}").json()
        assert run_state["status"] == "blocked"
        events = ienv.client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
        reasons = [e.get("reason_code") for e in events if e["event_type"] == "tool_request"]
        assert reasons == ["ALLOW", "PROTECTED_PATH", "RUN_NOT_ACTIVE"]


class TestScenarioB:
    """Unsafe patch: Impact Preview blocks it; approval/promotion refused; repo unchanged."""
    def test_blocked_patch_cannot_be_approved(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        d = ienv.authorize(run, contract, "read_file", {"path": "src/auth/session.ts"})
        assert d["decision"] == "allow"
        report = ienv.submit_impact(run, contract, UNSAFE_PATCH, "block",
                                     test_exit=1,
                                     findings=[{"severity": "critical", "code": "CRITICAL_TEST_FAILURE",
                                                "message": "Auth tests failed", "evidence": "auth-tests"}])
        status = ienv.get_status(report)
        assert status["decision"] == "block"
        assert not status["can_approve"]
        assert "IMPACT_BLOCKED" in status["approve_blockers"]
        resp = ienv.approve(report)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "IMPACT_BLOCKED"
        head = subprocess.check_output(["git", "-C", str(ienv.repo), "rev-parse", "HEAD"]).decode().strip()
        assert head == ienv.base_commit

    def test_blocked_patch_cannot_be_promoted(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        report = ienv.submit_impact(run, contract, UNSAFE_PATCH, "block",
                                     findings=[{"severity": "critical", "code": "CRITICAL_TEST_FAILURE",
                                                "message": "Auth tests failed", "evidence": "auth-tests"}])
        resp = ienv.promote_via_api(report)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "NOT_APPROVED"


class TestScenarioC:
    """Safe patch: passes Impact Preview, approved, promoted with hash verification."""
    def test_full_approve_promote_cycle(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        d = ienv.authorize(run, contract, "propose_patch", {"patch": SAFE_PATCH})
        assert d["decision"] == "allow"
        report = ienv.submit_impact(run, contract, SAFE_PATCH, "pass")
        status = ienv.get_status(report)
        assert status["decision"] == "pass"
        assert status["can_approve"]
        assert not status["approve_blockers"]
        resp = ienv.approve(report)
        assert resp.status_code == 200
        review = resp.json()["review"]
        assert review["decision"] == "approve"
        assert review["patch_sha256"] == report["patch_sha256"]
        assert review["base_commit"] == report["base_commit"]
        status2 = ienv.get_status(report)
        assert status2["review"] is not None
        assert status2["review"]["decision"] == "approve"

    def test_promoted_patch_applied_to_repo(self, ienv):
        import socket, uvicorn, time as _time
        sock = socket.socket(); sock.bind(("127.0.0.1", 0))
        api_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        handler = _make_promoter_handler(ienv.repo, ienv.artifacts, api_url, TOKEN)
        psock = socket.socket(); psock.bind(("127.0.0.1", 0))
        promoter_url = f"http://127.0.0.1:{psock.getsockname()[1]}"
        psock.close()
        pserver = ThreadingHTTPServer(("127.0.0.1", int(promoter_url.split(":")[-1])), handler)
        pt = threading.Thread(target=pserver.serve_forever, daemon=True); pt.start()
        promoter_client = PromoterClient(promoter_url, PROMOTER_TOKEN)
        app = create_app(ienv.store, internal_token=TOKEN, artifacts=ienv.reader,
                         promoter=promoter_client, reviewer_tokens={REVIEWER: REVIEWER_TOKEN})
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        st = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True); st.start()
        for _ in range(100):
            if server.started: break
            _time.sleep(0.05)
        try:
            import urllib.request as _ur
            def _post(path, body, extra_headers=None):
                headers = {"Content-Type": "application/json", "X-IntentLock-Internal-Token": TOKEN}
                if extra_headers: headers.update(extra_headers)
                req = _ur.Request(api_url + path, data=json.dumps(body).encode(), headers=headers, method="POST")
                with _ur.urlopen(req, timeout=10) as r: return json.loads(r.read()), r.status
            def _get(path):
                req = _ur.Request(api_url + path, headers={"X-IntentLock-Internal-Token": TOKEN})
                with _ur.urlopen(req, timeout=10) as r: return json.loads(r.read())
            d = json.loads((FIX / "contract-draft.json").read_text())
            d["base_commit"] = ienv.base_commit; d["allowed_paths"] = ["src/auth/**"]
            contract, _ = _post("/api/contracts", {"draft": d, "approved": True, "approved_by": "tester"})
            run, _ = _post("/api/runs", {"contract_id": contract["contract_id"], "contract_version": contract["version"]})
            iid = "impact_" + uuid.uuid4().hex[:12]
            key = f"runs/{run['run_id']}/patches/{SAFE_SHA}.patch"
            (ienv.artifacts / key).parent.mkdir(parents=True, exist_ok=True)
            (ienv.artifacts / key).write_text(SAFE_PATCH)
            report = {"impact_report_id": iid, "run_id": run["run_id"], "sandbox_id": "sandbox_test",
                      "contract_id": contract["contract_id"], "contract_version": contract["version"],
                      "base_commit": run["base_commit"], "patch_s3_key": key, "patch_sha256": SAFE_SHA,
                      "summary": {"files_modified": 1, "files_created": 0, "files_deleted": 0,
                                  "files_renamed": 0, "lines_added": 1, "lines_deleted": 1},
                      "tests": [{"test_id": "auth-tests", "status": "passed", "exit_code": 0,
                                  "duration_ms": 500, "output_s3_key": f"runs/{run['run_id']}/tests/auth-tests.txt"}],
                      "findings": [], "decision": "pass", "created_at": "2026-11-07T10:05:00Z"}
            _post("/internal/impact", report)
            _post(f"/api/impact/{iid}/approve",
                  {"reviewer": REVIEWER, "patch_sha256": SAFE_SHA, "base_commit": run["base_commit"]},
                  {"X-IntentLock-Reviewer-Token": REVIEWER_TOKEN, "X-IntentLock-Internal-Token": ""})
            result, _ = _post(f"/api/impact/{iid}/promote",
                              {"requested_by": REVIEWER, "patch_sha256": SAFE_SHA},
                              {"X-IntentLock-Reviewer-Token": REVIEWER_TOKEN, "X-IntentLock-Internal-Token": ""})
            assert result["promotion"]["status"] == "promoted", result
            assert result["promotion"]["applied_patch_sha256"] == SAFE_SHA
            content = (ienv.repo / "src" / "auth" / "session.ts").read_text()
            assert "timeoutSeconds * 1000" in content
        finally:
            server.should_exit = True; st.join(5); sock.close()
            pserver.shutdown(); pt.join(5); pserver.server_close()


class TestSecurityGuarantees:
    """Verify the security guarantees from the PRD at the API level."""
    def test_unauthorized_request_rejected(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        d = ienv.authorize(run, contract, "rm_rf", {})
        assert d["decision"] == "deny" and d["reason_code"] == "TOOL_NOT_ALLOWED"

    def test_protected_paths_inaccessible(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        for path in [".env", ".git/config", ".github/workflows/ci.yml", "infra/main.tf"]:
            d = ienv.authorize(run, contract, "read_file", {"path": path})
            assert d["decision"] == "deny", f"{path} was allowed"

    def test_stopped_run_blocks_approval_and_promotion(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        report = ienv.submit_impact(run, contract, SAFE_PATCH, "pass")
        ienv.approve(report)
        ienv.client.post(f"/api/runs/{run['run_id']}/stop", json={"reason": "user stopped"})
        status = ienv.get_status(report)
        assert "RUN_NOT_ELIGIBLE" in status["promote_blockers"]
        assert not status["can_promote"]

    def test_wrong_reviewer_token_rejected(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        report = ienv.submit_impact(run, contract, SAFE_PATCH, "pass")
        resp = ienv.client.post(
            f"/api/impact/{report['impact_report_id']}/approve",
            json={"reviewer": REVIEWER, "patch_sha256": report["patch_sha256"],
                  "base_commit": report["base_commit"]},
            headers={"X-IntentLock-Reviewer-Token": "wrong-token-here"})
        assert resp.status_code == 401

    def test_changed_patch_hash_fails_promotion(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        report = ienv.submit_impact(run, contract, SAFE_PATCH, "pass")
        ienv.approve(report)
        resp = ienv.client.post(
            f"/api/impact/{report['impact_report_id']}/promote",
            json={"requested_by": REVIEWER, "patch_sha256": "a" * 64},
            headers={"X-IntentLock-Reviewer-Token": REVIEWER_TOKEN})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "PATCH_HASH_MISMATCH"

    def test_events_contain_no_file_contents(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        ienv.authorize(run, contract, "read_file", {"path": "src/auth/session.ts"})
        report = ienv.submit_impact(run, contract, SAFE_PATCH, "pass")
        ienv.approve(report)
        events = ienv.client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
        dump = json.dumps(events)
        assert "demo-valid-token" not in dump
        assert "timeoutSeconds * 1000" not in dump

    def test_blocked_report_on_stopped_run(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        report = ienv.submit_impact(run, contract, UNSAFE_PATCH, "block",
                                     findings=[{"severity": "critical", "code": "CRITICAL_TEST_FAILURE",
                                                "message": "Tests failed", "evidence": "auth-tests"}])
        ienv.client.post(f"/api/runs/{run['run_id']}/stop", json={})
        resp = ienv.approve(report)
        assert resp.status_code == 409

    def test_internal_token_required(self, ienv):
        contract = ienv.create_contract()
        run = ienv.create_run(contract)
        body = {"request_id": "q1", "run_id": run["run_id"], "contract_id": contract["contract_id"],
                "contract_version": contract["version"], "tool": "read_file",
                "arguments": {"path": "src/auth/session.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"}
        resp = ienv.client.post("/internal/authorize", json=body)
        assert resp.status_code == 401
        resp = ienv.client.post("/internal/authorize", json=body, headers={"X-IntentLock-Internal-Token": "wrong"})
        assert resp.status_code == 401
