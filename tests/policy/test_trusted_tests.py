"""Agent patches must not be able to change the tests TwinGuard uses to judge them."""
import json
import pathlib

import pytest

from intentlock_policy import authorize
from intentlock_policy.models import ApprovedTest
from conftest import NOW

VEC = json.loads((pathlib.Path(__file__).resolve().parents[2] / "packages/contracts/fixtures/patch-vectors/trusted-tests.json").read_text())["vectors"]


def widen(contract, **over):
    # allowed_paths intentionally include tests/** etc.: the trusted-test rule must override the contract.
    return contract.model_copy(update={"allowed_paths": ["src/auth/**", "tests/**", "package.json", "jest.config.*", "scripts/**"], **over})


def decide(make_req, run, contract, patch):
    return authorize(make_req("propose_patch", {"patch": patch}), run, widen(contract), 0, NOW)


@pytest.mark.parametrize("v", VEC, ids=[v["name"] for v in VEC])
def test_shared_vectors(make_req, run, contract, v):
    d = decide(make_req, run, contract, v["patch"])
    exp = v["intentlock_expect"]
    assert (d.decision, d.reason_code) == (exp["decision"], exp["reason_code"]), d
    if "evidence_rule" in exp:
        assert d.evidence["rule"] == exp["evidence_rule"]
        assert d.stop_run is False


def hunk(path, minus=None, plus=None, extra=""):
    body = f"diff --git a/{path} b/{path}\n{extra}--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
    return body + (f"-{minus}\n" if minus is not None else "") + (f"+{plus}\n" if plus is not None else "")


def test_reading_tests_is_still_allowed(make_req, run, contract):
    d = authorize(make_req("read_file", {"path": "tests/auth/token.test.ts"}), run, contract, 0, NOW)
    assert d.decision == "allow"


def test_rename_in_or_out_of_test_dir_denied(make_req, run, contract):
    into = ("diff --git a/src/auth/a.ts b/tests/auth/a.test.ts\nsimilarity index 100%\nrename from src/auth/a.ts\nrename to tests/auth/a.test.ts\n")
    out = ("diff --git a/tests/auth/a.test.ts b/src/auth/a.ts\nsimilarity index 100%\nrename from tests/auth/a.test.ts\nrename to src/auth/a.ts\n")
    for patch in (into, out):
        assert decide(make_req, run, contract, patch).evidence["rule"] == "TRUSTED_TEST_READ_ONLY"


def test_case_insensitive_and_nested(make_req, run, contract):
    for path in ("src/auth/Token.Test.TS", "src/auth/__tests__/x.ts", "src/auth/tests/x.ts"):
        assert decide(make_req, run, contract, hunk(path, "a", "b")).evidence["rule"] == "TRUSTED_TEST_READ_ONLY", path


def test_script_names_come_from_approved_argv(make_req, run, contract):
    c = contract.model_copy(update={"approved_tests": {"sec": ApprovedTest(argv=["npm", "run", "verify:auth"], timeout_seconds=30)}})
    d = authorize(make_req("propose_patch", {"patch": hunk("package.json", '    "verify:auth": "jest"', '    "verify:auth": "true"')}), run, widen(c), 0, NOW)
    assert d.reason_code == "PATH_NOT_ALLOWED" and d.evidence["matched_rule"].startswith("script:verify:auth")
    d2 = authorize(make_req("propose_patch", {"patch": hunk("package.json", '    "preverify:auth": "x"', '    "preverify:auth": "y"')}), run, widen(c), 0, NOW)
    assert d2.reason_code == "PATH_NOT_ALLOWED"
    # an unrelated script is fine
    d3 = authorize(make_req("propose_patch", {"patch": hunk("package.json", '    "build": "tsc"', '    "build": "tsc -p ."')}), run, widen(c), 0, NOW)
    assert d3.decision == "allow"


def test_files_named_in_approved_argv_are_read_only(make_req, run, contract):
    c = contract.model_copy(update={"approved_tests": {"sec": ApprovedTest(argv=["node", "scripts/check-auth.js"], timeout_seconds=30)}})
    d = authorize(make_req("propose_patch", {"patch": hunk("scripts/check-auth.js", "a", "b")}), run, widen(c), 0, NOW)
    assert d.reason_code == "PATH_NOT_ALLOWED" and d.evidence["matched_rule"] == "approved_test_argv"
    assert authorize(make_req("propose_patch", {"patch": hunk("scripts/other.js", "a", "b")}), run, widen(c), 0, NOW).decision == "allow"


def test_package_json_file_operations_denied(make_req, run, contract):
    for extra in ("deleted file mode 100644\n", "new file mode 100644\n"):
        assert decide(make_req, run, contract, hunk("package.json", "{}", "{}", extra)).reason_code == "PATH_NOT_ALLOWED"


def test_protected_paths_still_take_precedence(make_req, run, contract):
    d = decide(make_req, run, contract, hunk(".env", "a", "b"))
    assert d.reason_code == "PROTECTED_PATH" and d.stop_run is True


def test_mixed_patch_with_one_test_file_is_denied_whole(make_req, run, contract):
    patch = hunk("src/auth/session.ts", "a", "b") + hunk("tests/auth/token.test.ts", "c", "d")
    assert decide(make_req, run, contract, patch).evidence["rule"] == "TRUSTED_TEST_READ_ONLY"


def test_evidence_never_contains_patch_content(make_req, run, contract):
    patch = hunk("tests/auth/token.test.ts", "OLDSECRET", "NEWSECRET")
    dumped = json.dumps(decide(make_req, run, contract, patch).model_dump(mode="json"))
    assert "OLDSECRET" not in dumped and "NEWSECRET" not in dumped
