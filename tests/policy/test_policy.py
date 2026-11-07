"""Security tests for the deterministic policy engine (PRD sections 12, 17)."""
import json
from datetime import timedelta

import pytest

from intentlock_policy import authorize
from intentlock_policy.models import AuthorizationDecision
from conftest import NOW


def run_auth(req, run, contract, prior=0, now=NOW):
    d = authorize(req, run, contract, prior, now)
    assert isinstance(d, AuthorizationDecision)
    return d


def patch_for(path, extra=""):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,1 +1,1 @@\n-old\n+new\n{extra}")


# ---- required acceptance cases ----------------------------------------
def test_allowed_session_ts(make_req, run, contract):
    d = run_auth(make_req("read_file", {"path": "src/auth/session.ts"}), run, contract)
    assert (d.decision, d.reason_code, d.stop_run) == ("allow", "ALLOW", False)


def test_env_is_protected_and_stops_run(make_req, run, contract):
    d = run_auth(make_req("read_file", {"path": ".env"}), run, contract)
    assert (d.decision, d.reason_code, d.stop_run) == ("deny", "PROTECTED_PATH", True)
    assert d.evidence["matched_rule"] in (".env*",)


def test_other_path_denied(make_req, run, contract):
    d = run_auth(make_req("read_file", {"path": "src/other/file.ts"}), run, contract)
    assert (d.decision, d.reason_code, d.stop_run) == ("deny", "PATH_NOT_ALLOWED", False)


def test_unknown_tool_denied(make_req, run, contract):
    assert run_auth(make_req("rm_rf", {}), run, contract).reason_code == "TOOL_NOT_ALLOWED"


def test_network_tool_denied(make_req, run, contract):
    assert run_auth(make_req("fetch", {"url": "http://x"}), run, contract).reason_code == "NETWORK_NOT_ALLOWED"


def test_unknown_test_id_denied(make_req, run, contract):
    d = run_auth(make_req("run_approved_test", {"test_id": "unknown"}), run, contract)
    assert d.reason_code == "TEST_NOT_APPROVED"
    assert run_auth(make_req("run_approved_test", {"test_id": "auth-tests"}), run, contract).decision == "allow"


def test_missing_contract_fails_closed(make_req, run):
    assert run_auth(make_req("read_file", {"path": "src/auth/a.ts"}), run, None).reason_code == "CONTRACT_NOT_FOUND"


def test_missing_run_fails_closed(make_req, contract):
    assert run_auth(make_req("read_file", {"path": "src/auth/a.ts"}), None, contract).reason_code == "RUN_NOT_ACTIVE"


def test_version_mismatch(make_req, run, contract):
    d = run_auth(make_req("read_file", {"path": "src/auth/a.ts"}, contract_version=2), run, contract)
    assert d.reason_code == "CONTRACT_VERSION_MISMATCH"


def test_contract_must_match_run_version(make_req, run, contract):
    c2 = contract.model_copy(update={"version": 2})
    assert run_auth(make_req("read_file", {"path": "src/auth/a.ts"}), run, c2).reason_code == "CONTRACT_VERSION_MISMATCH"


@pytest.mark.parametrize("status", ["superseded", "revoked"])
def test_inactive_contract_rejected(make_req, run, contract, status):
    c = contract.model_copy(update={"status": status})
    assert run_auth(make_req("read_file", {"path": "src/auth/a.ts"}), run, c).reason_code == "CONTRACT_NOT_ACTIVE"


@pytest.mark.parametrize("status", ["created", "stopped", "blocked", "completed", "failed"])
def test_non_active_run_rejected(make_req, run, contract, status):
    r = run.model_copy(update={"status": status})
    d = run_auth(make_req("read_file", {"path": "src/auth/a.ts"}), r, contract)
    assert d.reason_code == "RUN_NOT_ACTIVE"


def test_tool_call_limit(make_req, run, contract):
    req = make_req("read_file", {"path": "src/auth/a.ts"})
    assert run_auth(req, run, contract, prior=49).decision == "allow"
    assert run_auth(req, run, contract, prior=50).reason_code == "TOOL_CALL_LIMIT_EXCEEDED"


def test_run_time_limit(make_req, run, contract):
    req = make_req("read_file", {"path": "src/auth/a.ts"})
    ok = run_auth(req, run, contract, now=run.started_at + timedelta(seconds=599))
    late = run_auth(req, run, contract, now=run.started_at + timedelta(seconds=600))
    assert ok.decision == "allow"
    assert (late.decision, late.reason_code, late.stop_run) == ("deny", "RUN_NOT_ACTIVE", True)
    assert late.evidence["cause"] == "run_timeout"


# ---- path attacks ------------------------------------------------------
@pytest.mark.parametrize("path", [
    "../.env", "src/auth/../../.env", "/etc/passwd", "C:/Windows/x", "~/x",
    "src/auth/a\x00.ts", "src\\auth\\a.ts", "%2e%2e/.env", "src/auth/%2e%2e/%2e%2e/x",
    "%2fetc/passwd", "%252e%252e/x", "", ".", 5, None,
])
def test_invalid_paths_denied(make_req, run, contract, path):
    d = run_auth(make_req("read_file", {"path": path}), run, contract)
    assert d.decision == "deny" and d.reason_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".ENV", "src/auth/.env", "src/auth/.env.production",
    ".git/config", "src/auth/.git/x", ".github/workflows/ci.yml", "infra/main.tf",
    "src/auth/./../../.env".replace("..", "x/..") ,
])
def test_protected_paths(make_req, run, contract, path):
    d = run_auth(make_req("read_file", {"path": path}), run, contract)
    assert d.decision == "deny"
    assert d.reason_code in ("PROTECTED_PATH", "INVALID_ARGUMENTS")


def test_protected_takes_precedence_over_allowed(make_req, run, contract):
    c = contract.model_copy(update={"allowed_paths": ["**"]})
    assert run_auth(make_req("read_file", {"path": ".env"}), run, c).reason_code == "PROTECTED_PATH"
    assert run_auth(make_req("read_file", {"path": "src/auth/.env"}), run, c).reason_code == "PROTECTED_PATH"


def test_baseline_protection_cannot_be_removed_by_contract(make_req, run, contract):
    c = contract.model_copy(update={"protected_paths": [], "allowed_paths": ["**"]})
    assert run_auth(make_req("read_file", {"path": ".env"}), run, c).reason_code == "PROTECTED_PATH"
    assert run_auth(make_req("read_file", {"path": ".git/config"}), run, c).reason_code == "PROTECTED_PATH"


def test_allowed_patterns_are_root_anchored(make_req, run, contract):
    # 'package.json' is allowed at the root only.
    assert run_auth(make_req("read_file", {"path": "package.json"}), run, contract).decision == "allow"
    assert run_auth(make_req("read_file", {"path": "sub/package.json"}), run, contract).reason_code == "PATH_NOT_ALLOWED"
    assert run_auth(make_req("read_file", {"path": "x/src/auth/a.ts"}), run, contract).reason_code == "PATH_NOT_ALLOWED"


def test_nested_allowed_and_dot_normalisation(make_req, run, contract):
    assert run_auth(make_req("read_file", {"path": "src/auth/a/b/c.ts"}), run, contract).decision == "allow"
    assert run_auth(make_req("read_file", {"path": "./src//auth/./x.ts"}), run, contract).decision == "allow"
    assert run_auth(make_req("list_files", {"path": "src/auth"}), run, contract).decision == "allow"
    assert run_auth(make_req("list_files", {"path": "src"}), run, contract).reason_code == "PATH_NOT_ALLOWED"


# ---- argument validation ----------------------------------------------
def test_extra_or_missing_arguments_denied(make_req, run, contract):
    assert run_auth(make_req("read_file", {"path": "src/auth/a.ts", "x": 1}), run, contract).reason_code == "INVALID_ARGUMENTS"
    assert run_auth(make_req("read_file", {}), run, contract).reason_code == "INVALID_ARGUMENTS"


def test_malformed_request_denied(run, contract):
    for bad in (None, "x", [], {}, {"request_id": "r"}):
        d = run_auth(bad, run, contract)
        assert d.decision == "deny" and d.reason_code == "INVALID_ARGUMENTS"


def test_search_code(make_req, run, contract):
    ok = run_auth(make_req("search_code", {"query": "timeout", "paths": ["src/auth/**"]}), run, contract)
    assert ok.decision == "allow"
    for paths, code in [(["**"], "PATH_NOT_ALLOWED"), (["src/**"], "PATH_NOT_ALLOWED"),
                        ([".env"], "PROTECTED_PATH"), (["src/auth/**", ".git"], "PROTECTED_PATH"),
                        (["../x"], "INVALID_ARGUMENTS"), ([], "INVALID_ARGUMENTS"), ("src/auth", "INVALID_ARGUMENTS")]:
        assert run_auth(make_req("search_code", {"query": "t", "paths": paths}), run, contract).reason_code == code, paths


# ---- propose_patch -----------------------------------------------------
def test_patch_allowed(make_req, run, contract):
    d = run_auth(make_req("propose_patch", {"patch": patch_for("src/auth/session.ts")}), run, contract)
    assert d.decision == "allow", d


def test_patch_protected_path(make_req, run, contract):
    d = run_auth(make_req("propose_patch", {"patch": patch_for(".env")}), run, contract)
    assert (d.reason_code, d.stop_run) == ("PROTECTED_PATH", True)


def test_patch_outside_allowed(make_req, run, contract):
    d = run_auth(make_req("propose_patch", {"patch": patch_for("src/other/x.ts")}), run, contract)
    assert d.reason_code == "PATH_NOT_ALLOWED"


def test_patch_one_bad_file_denies_whole_patch(make_req, run, contract):
    patch = patch_for("src/auth/a.ts") + patch_for("src/payments/b.ts")
    assert run_auth(make_req("propose_patch", {"patch": patch}), run, contract).reason_code == "PATH_NOT_ALLOWED"
    patch = patch_for("src/auth/a.ts") + patch_for(".env.local")
    assert run_auth(make_req("propose_patch", {"patch": patch}), run, contract).reason_code == "PROTECTED_PATH"


def test_patch_rename_into_protected(make_req, run, contract):
    patch = ("diff --git a/src/auth/a.ts b/.env\nsimilarity index 100%\n"
             "rename from src/auth/a.ts\nrename to .env\n")
    assert run_auth(make_req("propose_patch", {"patch": patch}), run, contract).reason_code == "PROTECTED_PATH"


def test_patch_traversal_and_malformed(make_req, run, contract):
    for patch in [patch_for("src/auth/../../.env"), "not a patch", "", "   ",
                  "--- src/auth/a.ts\n+++ src/auth/a.ts\n@@ -1 +1 @@\n-a\n+b\n",   # missing a/ b/ prefix
                  'diff --git "a/src/auth/x y" "b/src/auth/x y"\n',
                  "diff --git a/src/auth/x y b/src/auth/x y\n"]:
        d = run_auth(make_req("propose_patch", {"patch": patch}), run, contract)
        assert d.reason_code == "INVALID_ARGUMENTS", patch[:40]


def test_patch_hidden_header_inside_hunk_is_still_checked(make_req, run, contract):
    # Hunk claims many lines so a naive hunk-aware parser would swallow the 2nd file header.
    patch = ("diff --git a/src/auth/a.ts b/src/auth/a.ts\n--- a/src/auth/a.ts\n+++ b/src/auth/a.ts\n"
             "@@ -1,50 +1,50 @@\n-x\n+y\n"
             "--- a/.env\n+++ b/.env\n")
    d = run_auth(make_req("propose_patch", {"patch": patch}), run, contract)
    assert d.reason_code == "PROTECTED_PATH"


def test_oversized_patch_denied(make_req, run, contract):
    big = patch_for("src/auth/a.ts", "+" + "x" * 1_100_000 + "\n")
    assert run_auth(make_req("propose_patch", {"patch": big}), run, contract).reason_code == "INVALID_ARGUMENTS"


# ---- tool allowlist semantics -----------------------------------------
def test_tool_not_in_contract_denied(make_req, run, contract):
    c = contract.model_copy(update={"allowed_tools": ["read_file"]})
    assert run_auth(make_req("propose_patch", {"patch": patch_for("src/auth/a.ts")}), run, c).reason_code == "TOOL_NOT_ALLOWED"


def test_always_allowed_tools_still_validate(make_req, run, contract):
    assert run_auth(make_req("finish_task", {"summary": "done"}), run, contract).decision == "allow"
    assert run_auth(make_req("request_permission", {"tool": "read_file", "resource": ".env", "reason": "need"}), run, contract).decision == "allow"
    assert run_auth(make_req("finish_task", {}), run, contract).reason_code == "INVALID_ARGUMENTS"


# ---- fail-closed + no leakage -----------------------------------------
def test_internal_error_fails_closed(make_req, run, contract, monkeypatch):
    import intentlock_policy.engine as eng
    monkeypatch.setattr(eng, "_check_paths", lambda *a, **k: 1 / 0)
    d = run_auth(make_req("read_file", {"path": "src/auth/a.ts"}), run, contract)
    assert (d.decision, d.reason_code) == ("deny", "POLICY_ENGINE_ERROR")


def test_decision_never_contains_patch_contents(make_req, run, contract):
    secret = "SUPERSECRETVALUE"
    patch = patch_for("src/other/x.ts").replace("+new", f"+{secret}")
    d = run_auth(make_req("propose_patch", {"patch": patch}), run, contract)
    assert secret not in json.dumps(d.model_dump(mode="json"))


def test_redaction_never_keeps_patch_or_secrets():
    from intentlock_policy.redact import redact_arguments, sanitize_details
    r = redact_arguments("propose_patch", {"patch": "+AKIAABCDEFGHIJKLMNOP secret"})
    assert set(r) == {"patch_sha256", "patch_bytes"}
    assert "AKIA" not in json.dumps(redact_arguments("search_code", {"query": "AKIAABCDEFGHIJKLMNOP", "paths": []}))
    assert sanitize_details({"patch": "x", "n": 1, "t": "a" * 40})["patch"] == "[REDACTED]"
    assert "a" * 40 not in json.dumps(sanitize_details({"t": "a" * 40}))


def test_decisions_are_deterministic(make_req, run, contract):
    req = make_req("read_file", {"path": ".env"})
    a, b = run_auth(req, run, contract), run_auth(req, run, contract)
    strip = lambda d: {**d.model_dump(), "latency_ms": 0}
    assert strip(a) == strip(b)


def test_always_allowed_tools_obey_run_state_limits_and_args(make_req, run, contract):
    for tool, args in (("finish_task", {"summary": "done"}),
                       ("request_permission", {"tool": "read_file", "resource": ".env", "reason": "r"})):
        assert run_auth(make_req(tool, args), run, contract).decision == "allow"
        assert run_auth(make_req(tool, args), run.model_copy(update={"status": "stopped"}), contract).reason_code == "RUN_NOT_ACTIVE"
        assert run_auth(make_req(tool, args), run, contract.model_copy(update={"status": "revoked"})).reason_code == "CONTRACT_NOT_ACTIVE"
        assert run_auth(make_req(tool, args, contract_version=9), run, contract).reason_code == "CONTRACT_VERSION_MISMATCH"
        assert run_auth(make_req(tool, args), run, contract, prior=50).reason_code == "TOOL_CALL_LIMIT_EXCEEDED"
        assert run_auth(make_req(tool, {**args, "extra": 1}), run, contract).reason_code == "INVALID_ARGUMENTS"
