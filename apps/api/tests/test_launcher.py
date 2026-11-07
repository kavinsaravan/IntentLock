"""Launch API + watcher tests.

SYNTHETIC: the commands launched here are tiny stand-in scripts (NOT Kiro, NOT an LLM). They exercise the
control plane's process lifecycle handling only. Real-runner tests live in tests/integration/.
"""
import json
import os
import signal
import sys
import time

import pytest
from fastapi.testclient import TestClient

from intentlock_api.app import create_app
from intentlock_api.launcher import AgentLauncher
from test_api import FIX, H, TOKEN, activate, authz, req, start_run  # shared helpers


def wait_until(fn, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


def script(tmp_path, body, name="agent.py"):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def make(store, tmp_path, argv, **extra):
    env = {"INTENTLOCK_AGENT_LAUNCH_ARGV": json.dumps(argv), "INTENTLOCK_API_URL": "http://127.0.0.1:1",
           "INTENTLOCK_INTERNAL_TOKEN": "tok-for-child", "INTENTLOCK_AGENT_LOG_DIR": str(tmp_path / "logs"),
           "INTENTLOCK_AGENT_STOP_GRACE_SECONDS": "0.3", "PATH": os.environ.get("PATH", ""), **extra}
    launcher = AgentLauncher(store, env=env)
    return launcher, TestClient(create_app(store, launcher=launcher, internal_token=TOKEN))


@pytest.fixture(autouse=True)
def reap():
    made = []
    yield made
    for launcher in made:
        for rec in launcher._records.values():
            try:
                os.killpg(rec.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def setup(client):
    c = activate(client)
    return c, start_run(client, c)


def test_not_configured_returns_503_and_info(store):
    client = TestClient(create_app(store, launcher=AgentLauncher(store, env={}), internal_token=TOKEN))
    assert client.get("/api/launcher").json()["configured"] is False
    c, run = setup(client)
    r = client.post(f"/api/runs/{run['run_id']}/launch")
    assert r.status_code == 503 and "AGENT_LAUNCHER_NOT_CONFIGURED" in r.json()["detail"]
    assert client.get(f"/api/runs/{run['run_id']}/launch").json() == {"launched": False}


@pytest.mark.parametrize("raw,why", [("not json", "invalid"), ('["a", {"x": 1}]', "invalid"), ("[]", "invalid"),
                                     ('["x", "{bad}"]', "unknown placeholder"), ('["x", "{0}"]', "unknown placeholder")])
def test_bad_launch_config_is_rejected(store, raw, why):
    lc = AgentLauncher(store, env={"INTENTLOCK_AGENT_LAUNCH_ARGV": raw, "INTENTLOCK_API_URL": "u", "INTENTLOCK_INTERNAL_TOKEN": "t"})
    assert not lc.configured and why in lc.problem


def test_requires_api_url_and_token(store):
    lc = AgentLauncher(store, env={"INTENTLOCK_AGENT_LAUNCH_ARGV": '["x"]'})
    assert not lc.configured and "INTENTLOCK_API_URL" in lc.problem


def test_launch_passes_ids_mission_and_minimal_env(store, tmp_path, reap):
    out = tmp_path / "seen.json"
    agent = script(tmp_path, f"import json,os,sys,time\njson.dump({{'argv':sys.argv[1:],'env':dict(os.environ)}},open({str(out)!r},'w'))\ntime.sleep(60)\n")
    launcher, client = make(store, tmp_path, [sys.executable, agent, "--run-id", "{run_id}", "--contract-id", "{contract_id}",
                                              "--contract-version", "{contract_version}", "--prompt={mission}"],
                          AWS_ACCESS_KEY_ID="AKIASHOULDNOTLEAK00", AWS_PROFILE="shouldnotleak",
                          INTENTLOCK_AGENT_ENV_PASSTHROUGH="AWS_ACCESS_KEY_ID,AWS_PROFILE")   # even if explicitly requested
    reap.append(launcher)
    c, run = setup(client)
    r = client.post(f"/api/runs/{run['run_id']}/launch")
    assert r.status_code == 201 and r.json()["launched"] is True
    seen = json.loads(wait_until(lambda: out.exists() and out.read_text() or None))
    assert seen["argv"][:6] == ["--run-id", run["run_id"], "--contract-id", c["contract_id"], "--contract-version", "1"]
    assert seen["argv"][6] == f"--prompt={c['mission']}"
    assert seen["env"]["INTENTLOCK_INTERNAL_TOKEN"] == "tok-for-child" and seen["env"]["INTENTLOCK_API_URL"] == "http://127.0.0.1:1"
    assert not any(k.upper().startswith("AWS_") for k in seen["env"])           # ambient AWS credentials never forwarded
    assert "tok-for-child" not in json.dumps(seen["argv"])                      # token only via env
    st = client.get(f"/api/runs/{run['run_id']}/launch").json()
    assert st["launched"] and st["alive"] and st["exit_code"] is None
    evs = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    assert any(e["event_type"] == "agent_event" and e["details"].get("launcher_pid") == st["launcher_pid"] for e in evs)
    assert client.post(f"/api/runs/{run['run_id']}/launch").status_code == 409   # one launch per run


def test_cannot_launch_inactive_or_unknown_run(store, tmp_path, reap):
    launcher, client = make(store, tmp_path, [sys.executable, "-c", "pass"])
    reap.append(launcher)
    c, run = setup(client)
    client.post(f"/api/runs/{run['run_id']}/stop")
    assert client.post(f"/api/runs/{run['run_id']}/launch").status_code == 409
    assert client.post("/api/runs/run_nope/launch").status_code == 404


def test_stop_terminates_launcher_and_state_is_committed_first(store, tmp_path, reap):
    agent = script(tmp_path, "import time\ntime.sleep(60)\n")
    launcher, client = make(store, tmp_path, [sys.executable, agent])
    reap.append(launcher)
    c, run = setup(client)
    client.post(f"/api/runs/{run['run_id']}/launch")
    assert launcher.status(run["run_id"])["alive"]
    r = client.post(f"/api/runs/{run['run_id']}/stop").json()
    assert r["state_committed"] is True and r["run"]["status"] == "stopped" and r["process_termination"] == "launcher_watcher_armed"
    wait_until(lambda: not launcher.status(run["run_id"])["alive"])
    st = launcher.status(run["run_id"])
    assert "SIGINT" in st["escalation"]
    wait_until(lambda: any(e["details"] and "exit_code" in e["details"] for e in client.get(f"/api/runs/{run['run_id']}/events").json()["events"] if e["event_type"] == "agent_event"))
    got = client.get(f"/api/runs/{run['run_id']}").json()
    assert got["status"] == "stopped" and got["stop_reason"] != "agent launcher exited with code 1"   # exit did not overwrite the stop
    assert authz(client, req(run, c, "read_file", {"path": "src/auth/a.ts"})).json()["reason_code"] == "RUN_NOT_ACTIVE"


def test_stubborn_runner_is_killed(store, tmp_path, reap):
    agent = script(tmp_path, "import signal,time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.1)\n")
    launcher, client = make(store, tmp_path, [sys.executable, agent], INTENTLOCK_AGENT_STOP_GRACE_SECONDS="0.2")
    reap.append(launcher)
    c, run = setup(client)
    client.post(f"/api/runs/{run['run_id']}/launch")
    time.sleep(0.5)                                            # let it install its handlers
    client.post(f"/api/runs/{run['run_id']}/stop")
    wait_until(lambda: not launcher.status(run["run_id"])["alive"], timeout=12)
    st = launcher.status(run["run_id"])
    assert st["exit_code"] == -signal.SIGKILL and st["escalation"] == ["SIGINT", "SIGTERM", "SIGKILL"]


def test_blocked_run_also_terminates_runner(store, tmp_path, reap):
    agent = script(tmp_path, "import time\ntime.sleep(60)\n")
    launcher, client = make(store, tmp_path, [sys.executable, agent])
    reap.append(launcher)
    c, run = setup(client)
    client.post(f"/api/runs/{run['run_id']}/launch")
    d = authz(client, req(run, c, "read_file", {"path": ".env"})).json()
    assert d["reason_code"] == "PROTECTED_PATH" and d["stop_run"] is True
    wait_until(lambda: not launcher.status(run["run_id"])["alive"])
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "blocked"


@pytest.mark.parametrize("code,status", [(0, "completed"), (3, "failed")])
def test_runner_exit_sets_terminal_state_when_still_active(store, tmp_path, reap, code, status):
    agent = script(tmp_path, f"import sys\nsys.exit({code})\n")
    launcher, client = make(store, tmp_path, [sys.executable, agent])
    reap.append(launcher)
    c, run = setup(client)
    client.post(f"/api/runs/{run['run_id']}/launch")
    wait_until(lambda: client.get(f"/api/runs/{run['run_id']}").json()["status"] == status)
    got = client.get(f"/api/runs/{run['run_id']}").json()
    assert (got["stop_reason"] is None) == (code == 0)
    evs = client.get(f"/api/runs/{run['run_id']}/events").json()["events"]
    assert [e["event_type"] for e in evs].count("run_completed") == 1


def test_runner_that_reported_state_itself_is_not_overwritten(store, tmp_path, reap):
    c_ = None
    agent = script(tmp_path, "import time\ntime.sleep(1.0)\nimport sys\nsys.exit(5)\n")
    launcher, client = make(store, tmp_path, [sys.executable, agent])
    reap.append(launcher)
    c, run = setup(client)
    client.post(f"/api/runs/{run['run_id']}/launch")
    client.post(f"/internal/runs/{run['run_id']}/state", headers=H, json={"status": "completed", "reason": "agent reported"})
    wait_until(lambda: not launcher.status(run["run_id"])["alive"], timeout=10)
    time.sleep(0.3)
    got = client.get(f"/api/runs/{run['run_id']}").json()
    assert got["status"] == "completed" and got["stop_reason"] == "agent reported"


def test_spawn_failure_marks_run_failed(store, tmp_path, reap):
    launcher, client = make(store, tmp_path, ["/nonexistent/intentlock-runner"])
    reap.append(launcher)
    c, run = setup(client)
    r = client.post(f"/api/runs/{run['run_id']}/launch")
    assert r.status_code == 502 and "AGENT_LAUNCH_FAILED" in r.json()["detail"]
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "failed"
