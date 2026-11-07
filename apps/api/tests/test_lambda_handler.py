"""Lambda handler tests. State is a MOCKED DynamoDB (moto); no real AWS is contacted."""
import json
import pathlib
import subprocess
import sys
import time
from datetime import timedelta

import pytest

import intentlock_api.lambda_handler as lh
from intentlock_api.store import create_store_from_env, now, StoreError
from intentlock_policy.models import AuthorizationDecision, MissionContract, Run

FIX = pathlib.Path(__file__).resolve().parents[3] / "packages" / "contracts" / "fixtures"
ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture
def env(moto_store, monkeypatch):
    monkeypatch.setattr(lh, "_store", moto_store)           # handler uses the mocked table
    return moto_store


def seed(store, *, status="active", started_offset=0, limits=None, cstatus="active"):
    d = json.loads((FIX / "contract.json").read_text())
    d.update(contract_id=f"c_{time.time_ns()}", version=1, status=cstatus)
    if limits: d["limits"].update(limits)
    c = MissionContract.model_validate(d); store.put_contract(c)
    t = now() + timedelta(seconds=started_offset)
    r = Run(run_id=f"run_{time.time_ns()}", contract_id=c.contract_id, contract_version=1, status=status,
            base_commit=c.base_commit, created_at=t, started_at=t)
    store.put_run(r)
    return c, r


def ev(c, r, tool="read_file", args=None, rid="req_1", **over):
    e = {"request_id": rid, "run_id": r.run_id, "contract_id": c.contract_id, "contract_version": c.version, "tool": tool,
         "arguments": args or {"path": "src/auth/session.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"}
    e.update(over); return e


def test_direct_invoke_allow_returns_decision_schema(env):
    c, r = seed(env)
    out = lh.handler(ev(c, r))
    AuthorizationDecision.model_validate(out)                    # exact shared schema
    assert (out["decision"], out["reason_code"]) == ("allow", "ALLOW")
    events = env.list_events(r.run_id)
    assert [(e.event_type, e.status, e.decision) for e in events] == [("tool_request", "decided", "allow")]


def test_protected_path_denies_stops_run_and_replay_is_denied(env):
    c, r = seed(env)
    d = lh.handler(ev(c, r, args={"path": ".env"}, rid="r_env"))
    assert (d["reason_code"], d["stop_run"]) == ("PROTECTED_PATH", True)
    assert env.get_run(r.run_id).status == "blocked"
    d2 = lh.handler(ev(c, r, rid="r_after"))
    assert d2["reason_code"] == "RUN_NOT_ACTIVE"
    kinds = [(e.event_type, e.reason_code) for e in env.list_events(r.run_id)]
    assert kinds == [("tool_request", "PROTECTED_PATH"), ("run_stopped", None), ("tool_request", "RUN_NOT_ACTIVE")]


def test_version_mismatch_and_inactive_contract(env):
    c, r = seed(env)
    assert lh.handler(ev(c, r, contract_version=2))["reason_code"] == "CONTRACT_VERSION_MISMATCH"
    c2, r2 = seed(env, cstatus="superseded")
    assert lh.handler(ev(c2, r2))["reason_code"] == "CONTRACT_NOT_ACTIVE"


def test_expired_run_becomes_inactive_before_denial(env):
    c, r = seed(env, started_offset=-700)                         # timeout is 600s
    d = lh.handler(ev(c, r, args={"path": "../x"}))               # also an invalid request
    assert d["reason_code"] == "RUN_NOT_ACTIVE" and d["evidence"]["run_status"] == "stopped"
    got = env.get_run(r.run_id)
    assert got.status == "stopped" and got.stop_reason == "run_timeout"


def test_tool_call_limit(env):
    c, r = seed(env, limits={"max_tool_calls": 2})
    codes = [lh.handler(ev(c, r, rid=f"q{i}"))["reason_code"] for i in range(3)]
    assert codes == ["ALLOW", "ALLOW", "TOOL_CALL_LIMIT_EXCEEDED"]


def test_unknown_run_and_malformed_events_fail_closed(env):
    c, r = seed(env)
    ghost = ev(c, r, run_id="run_nope")
    assert lh.handler(ghost)["reason_code"] == "RUN_NOT_ACTIVE"
    for bad in (None, "x", 5, [], {}, {"request_id": "q"}, {"body": "{not json", "headers": {}}):
        out = lh.handler(bad)
        body = json.loads(out["body"]) if "statusCode" in out else out
        assert body["decision"] == "deny"


def test_function_url_proxy_event(env):
    c, r = seed(env)
    event = {"version": "2.0", "headers": {}, "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(ev(c, r)), "isBase64Encoded": False}
    out = lh.handler(event)
    assert out["statusCode"] == 200
    assert json.loads(out["body"])["decision"] == "allow"
    import base64
    event.update(body=base64.b64encode(json.dumps(ev(c, r, rid="b64")).encode()).decode(), isBase64Encoded=True)
    assert json.loads(lh.handler(event)["body"])["request_id"] == "b64"


def test_store_failure_fails_closed(env, monkeypatch):
    c, r = seed(env)
    monkeypatch.setattr(env, "get_run", lambda *_: (_ for _ in ()).throw(RuntimeError("ddb unavailable")))
    out = lh.handler(ev(c, r, rid="req_down"))
    assert (out["decision"], out["reason_code"], out["request_id"]) == ("deny", "POLICY_ENGINE_ERROR", "req_down")


def test_misconfigured_lambda_denies_everything(monkeypatch):
    monkeypatch.setattr(lh, "_store", None)
    monkeypatch.delenv("INTENTLOCK_STORE", raising=False)
    monkeypatch.delenv("INTENTLOCK_ALLOW_MEMORY_STORE", raising=False)
    out = lh.handler({"request_id": "z", "run_id": "r", "contract_id": "c", "contract_version": 1, "tool": "read_file",
                      "arguments": {"path": "src/auth/a.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"})
    assert (out["decision"], out["reason_code"]) == ("deny", "POLICY_ENGINE_ERROR")


def test_handler_never_loads_bedrock_module():
    code = ("import sys; sys.path[:0]=['packages/policy-engine','apps/api']\n"
            "import intentlock_api.lambda_handler as h\n"
            "assert 'intentlock_api.bedrock' not in sys.modules, 'bedrock imported'\n"
            "assert 'intentlock_api.app' not in sys.modules\n"
            "import inspect; src=inspect.getsource(h)+inspect.getsource(__import__('intentlock_api.service',fromlist=['x']))\n"
            "assert 'bedrock' not in src.lower().replace('never calls bedrock','').replace('never imports or calls bedrock',''), 'bedrock referenced'\n"
            "print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout


def test_decisions_logged_without_contents(env, caplog):
    c, r = seed(env)
    with caplog.at_level("INFO", logger="intentlock.audit"):
        lh.handler(ev(c, r, args={"path": ".env"}))
    assert "PROTECTED_PATH" in caplog.text and ".env" not in caplog.text.replace("PROTECTED_PATH", "")


def test_config_factory():
    assert create_store_from_env({}).backend.startswith("local-memory")
    assert create_store_from_env({"INTENTLOCK_STORE": "memory"}).backend.startswith("local-memory")
    with pytest.raises(StoreError):
        create_store_from_env({"INTENTLOCK_STORE": "dynamodb"})          # table name required
    with pytest.raises(StoreError):
        create_store_from_env({"INTENTLOCK_STORE": "mysql"})


def test_factory_dynamodb(moto_store, monkeypatch):
    s = create_store_from_env({"INTENTLOCK_STORE": "dynamodb", "INTENTLOCK_TABLE": "il-test", "AWS_REGION": "us-east-1"})
    assert s.backend == "dynamodb:il-test@us-east-1"


def test_region_defaults_to_us_east_1_not_the_ambient_profile_region():
    from intentlock_api.store import aws_region
    assert aws_region({}) == "us-east-1"
    assert aws_region({"AWS_DEFAULT_REGION": "us-west-1"}) == "us-west-1"      # explicit env still wins
    assert aws_region({"AWS_REGION": "us-east-2", "AWS_DEFAULT_REGION": "us-west-1"}) == "us-east-2"
    s = create_store_from_env({"INTENTLOCK_STORE": "dynamodb", "INTENTLOCK_TABLE": "t"})
    assert s.backend == "dynamodb:t@us-east-1"
