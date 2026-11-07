"""run_demo_api.sh must refuse unsafe configuration. Uses --check, so no server and no AWS call is made."""
import os
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "run_demo_api.sh"
GOOD_INTERNAL = "i" * 32
GOOD_REVIEWERS = '{"demo-user": "' + "r" * 32 + '"}'


def run(**env):
    base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "PYTHON": os.environ.get("PYTHON", "")}
    base = {k: v for k, v in base.items() if v}
    base.update(env)
    r = subprocess.run(["bash", str(SCRIPT), "--check"], env=base, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def good(**over):
    e = {"INTENTLOCK_INTERNAL_TOKEN": GOOD_INTERNAL, "INTENTLOCK_REVIEWER_TOKENS": GOOD_REVIEWERS}
    e.update(over)
    return e


def test_valid_memory_config_passes_and_labels_the_fallback():
    rc, out = run(**good())
    assert rc == 0 and "in-memory LOCAL FALLBACK" in out and "127.0.0.1:8787" in out and "reviewers configured: demo-user" in out


@pytest.mark.parametrize("env,msg", [
    ({"INTENTLOCK_REVIEWER_TOKENS": GOOD_REVIEWERS}, "INTENTLOCK_INTERNAL_TOKEN is not set"),
    ({"INTENTLOCK_INTERNAL_TOKEN": "short", "INTENTLOCK_REVIEWER_TOKENS": GOOD_REVIEWERS}, "too short"),
    ({"INTENTLOCK_INTERNAL_TOKEN": "change-me-" + "x" * 30, "INTENTLOCK_REVIEWER_TOKENS": GOOD_REVIEWERS}, "placeholder"),
    ({"INTENTLOCK_INTERNAL_TOKEN": GOOD_INTERNAL}, "INTENTLOCK_REVIEWER_TOKENS is not set"),
    ({"INTENTLOCK_INTERNAL_TOKEN": GOOD_INTERNAL, "INTENTLOCK_REVIEWER_TOKENS": '{"a":"short"}'}, "invalid"),
    ({"INTENTLOCK_INTERNAL_TOKEN": GOOD_INTERNAL, "INTENTLOCK_REVIEWER_TOKENS": '{"a":"replace-with-random-secret-123456"}'}, "invalid"),
])
def test_refuses_missing_or_weak_secrets(env, msg):
    rc, out = run(**env)
    assert rc == 2 and "REFUSING TO START" in out and msg in out


def test_refuses_non_loopback_bind_unless_explicitly_allowed():
    rc, out = run(**good(HOST="0.0.0.0"))
    assert rc == 2 and "not loopback" in out
    rc, out = run(**good(HOST="0.0.0.0", ALLOW_NON_LOCAL="1"))
    assert rc == 0


def test_dynamodb_mode_requires_a_named_non_default_profile_and_us_east_1():
    rc, out = run(**good(INTENTLOCK_STORE="dynamodb"))
    assert rc == 2 and "AWS_PROFILE" in out
    rc, out = run(**good(INTENTLOCK_STORE="dynamodb", AWS_PROFILE="default"))
    assert rc == 2 and "not allowed" in out
    rc, out = run(**good(INTENTLOCK_STORE="dynamodb", AWS_PROFILE="intentlock", AWS_REGION="us-west-2"))
    assert rc == 2 and "us-east-1 only" in out
    rc, out = run(**good(INTENTLOCK_STORE="bogus"))
    assert rc == 2 and "must be 'memory' or 'dynamodb'" in out


def test_dynamodb_mode_passes_checks_without_touching_aws_when_skipped():
    rc, out = run(**good(INTENTLOCK_STORE="dynamodb", AWS_PROFILE="intentlock", SKIP_AWS_CHECK="1"))
    assert rc == 0 and "config OK" in out
