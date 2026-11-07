"""End-to-end: dashboard launch API -> REAL Person 2 runner -> MCP server -> REAL authorization -> events.

WHAT IS REAL: my FastAPI control plane, my launcher, my policy engine, Person 2's launch_kiro.py, supervisor
(lifecycle.py), control-plane client and MCP stdio server (server.py), real subprocesses and process groups,
real HTTP, real SSE.

WHAT IS SYNTHETIC: the "kiro-cli" executable. Kiro is not installed/logged in on this machine, so a
stand-in script plays the agent: it reads the SAME generated profile (.kiro/agents/intentlock.json) that
Kiro would, starts the MCP server exactly as the profile says, and sends scripted tools/call requests.
There is NO LLM and NO real Kiro here. The opt-in test at the bottom is the real-Kiro one (not run here).

Run:  INTENTLOCK_PERSON2_CHECKOUT=/path/to/checkout-of-origin/codex/person2-execution-plane \
      python -m pytest tests/integration -q
"""
import json
import os
import pathlib
import shutil
import signal
import socket
import stat
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from intentlock_api.app import create_app
from intentlock_api.launcher import AgentLauncher
from intentlock_api.store import LocalMemoryStore

P2 = os.environ.get("INTENTLOCK_PERSON2_CHECKOUT")
ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = ROOT / "packages/contracts/fixtures"
TOKEN = "e2e-token"
pytestmark = pytest.mark.skipif(not P2, reason="Set INTENTLOCK_PERSON2_CHECKOUT to a checkout of Person 2's branch")

STANDIN = '''# SYNTHETIC stand-in for kiro-cli. Not an LLM. Scripted MCP client driven by the real generated profile.
import json, os, subprocess, sys, time
mode = os.environ.get("STANDIN_MODE", "scenario_a")
if mode == "notlogged":
    print("Not logged in"); sys.exit(1)
profile = json.load(open(os.path.join(os.getcwd(), ".kiro", "agents", "intentlock.json")))
srv = profile["mcpServers"]["intentlock"]
env = dict(os.environ); env.update({k: os.path.expandvars(v) for k, v in srv["env"].items()})
mcp = subprocess.Popen([srv["command"], *srv["args"]], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
open(os.environ["STANDIN_PIDFILE"], "w").write(json.dumps({"agent": os.getpid(), "mcp": mcp.pid, "argv": sys.argv[1:]}))
results = []
def call(i, path):
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": path}}}) + "\\n"); mcp.stdin.flush()
    r = json.loads(mcp.stdout.readline())["result"]
    results.append({"path": path, "isError": r["isError"], "text": r["content"][0]["text"][:40] if r["isError"] else "<content withheld>"})
    open(os.environ["STANDIN_RESULTS"], "w").write(json.dumps(results))
call(1, "src/auth/session.ts")
if mode == "scenario_a":
    call(2, ".env"); call(3, "src/auth/session.ts")
elif mode == "long":
    time.sleep(120)
sys.exit(0)
'''


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    standin = tmp / "kiro-cli"
    (tmp / "standin.py").write_text(STANDIN)
    # sh wrapper: a Python shebang breaks when the interpreter path contains spaces
    standin.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp / "standin.py"}" "$@"\n')
    standin.chmod(standin.stat().st_mode | stat.S_IEXEC)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    store = LocalMemoryStore()
    argv = [sys.executable, "-B", f"{P2}/apps/mcp-server/launch_kiro.py", "--repository", f"{P2}/demo/repository",
            "--run-id", "{run_id}", "--contract-id", "{contract_id}", "--contract-version", "{contract_version}", "--prompt={mission}"]
    env = {"INTENTLOCK_AGENT_LAUNCH_ARGV": json.dumps(argv), "INTENTLOCK_API_URL": url, "INTENTLOCK_INTERNAL_TOKEN": TOKEN,
           "KIRO_CLI_BINARY": str(standin), "PATH": os.environ["PATH"], "HOME": str(tmp),
           "INTENTLOCK_AGENT_ENV_PASSTHROUGH": "STANDIN_MODE,STANDIN_PIDFILE,STANDIN_RESULTS", "INTENTLOCK_AGENT_LOG_DIR": str(tmp / "logs"),
           "INTENTLOCK_AGENT_STOP_GRACE_SECONDS": "5"}
    launcher_env = dict(env)
    launcher = AgentLauncher(store, env=launcher_env)
    server = uvicorn.Server(uvicorn.Config(create_app(store, launcher=launcher, internal_token=TOKEN), log_level="error"))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True); t.start()
    for _ in range(100):
        if server.started: break
        time.sleep(0.05)
    else:
        raise RuntimeError("API did not start")
    yield {"url": url, "launcher": launcher, "tmp": tmp, "store": store, "env": launcher_env}
    for rec in launcher._records.values():
        try: os.killpg(rec.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass
    server.should_exit = True; t.join(5); sock.close()


def api(stack, method, path, **kw):
    return httpx.request(method, stack["url"] + path, timeout=10, **kw)


def wait_until(fn, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v: return v
        time.sleep(0.1)
    raise AssertionError("timeout")


def new_run(stack, mode, tmp_name):
    d = json.loads((FIX / "contract-draft.json").read_text())
    c = api(stack, "POST", "/api/contracts", json={"draft": d, "approved": True, "approved_by": "e2e"}).json()
    run = api(stack, "POST", "/api/runs", json={"contract_id": c["contract_id"], "contract_version": 1}).json()
    pidfile, results = stack["tmp"] / f"{tmp_name}.pid.json", stack["tmp"] / f"{tmp_name}.results.json"
    # the launcher's env allowlist is read at construction; refresh the stand-in knobs for this run
    stack["launcher"]._passthrough.update(STANDIN_MODE=mode, STANDIN_PIDFILE=str(pidfile), STANDIN_RESULTS=str(results))
    return c, run, pidfile, results


def tool_events(stack, run_id):
    evs = api(stack, "GET", f"/api/runs/{run_id}/events").json()["events"]
    return evs, [e for e in evs if e["event_type"] == "tool_request"]


def alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False


def test_launch_api_starts_real_runner_and_scenario_a_is_enforced(stack):
    c, run, pidfile, results = new_run(stack, "scenario_a", "a")
    r = api(stack, "POST", f"/api/runs/{run['run_id']}/launch")
    assert r.status_code == 201, r.text
    wait_until(lambda: api(stack, "GET", f"/api/runs/{run['run_id']}").json()["status"] == "blocked")
    wait_until(lambda: not api(stack, "GET", f"/api/runs/{run['run_id']}/launch").json()["alive"])
    evs, tools = tool_events(stack, run["run_id"])
    assert [e["reason_code"] for e in tools] == ["ALLOW", "PROTECTED_PATH", "RUN_NOT_ACTIVE"]
    assert [e["tool"] for e in tools] == ["read_file"] * 3 and all(e["status"] == "decided" for e in tools)
    seen = json.loads(results.read_text())
    assert [x["isError"] for x in seen] == [False, True, True]                       # denied calls never returned file content
    assert [x["text"] for x in seen][1:] == ["PROTECTED_PATH", "RUN_NOT_ACTIVE"]
    run_after = api(stack, "GET", f"/api/runs/{run['run_id']}").json()
    assert run_after["status"] == "blocked" and run_after["agent_process_id"]          # Person 2's supervisor registered the PID
    assert "demo-valid-token" not in json.dumps(evs)                                   # no file contents in events
    assert any(e["event_type"] == "agent_event" and e["details"] and "launcher_pid" in e["details"] for e in evs)
    # UI/SSE accuracy: the SSE stream carries exactly the events the list endpoint returns
    with httpx.stream("GET", f"{stack['url']}/api/runs/{run['run_id']}/events/stream?timeout_seconds=0.6", timeout=10) as s:
        body = "".join(s.iter_text())
    sse = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    assert [e["seq"] for e in sse] == [e["seq"] for e in evs] and [e["event_id"] for e in sse] == [e["event_id"] for e in evs]


def test_clean_run_completes_when_runner_exits_zero(stack):
    c, run, pidfile, results = new_run(stack, "clean", "clean")
    assert api(stack, "POST", f"/api/runs/{run['run_id']}/launch").status_code == 201
    wait_until(lambda: api(stack, "GET", f"/api/runs/{run['run_id']}").json()["status"] == "completed")
    _, tools = tool_events(stack, run["run_id"])
    assert [e["reason_code"] for e in tools] == ["ALLOW"]
    assert api(stack, "POST", "/internal/authorize", headers={"X-IntentLock-Internal-Token": TOKEN}, json={
        "request_id": "late", "run_id": run["run_id"], "contract_id": c["contract_id"], "contract_version": 1, "tool": "read_file",
        "arguments": {"path": "src/auth/session.ts"}, "agent_id": "a", "timestamp": "2026-11-07T10:00:00Z"}).json()["reason_code"] == "RUN_NOT_ACTIVE"


def test_stop_run_terminates_real_supervisor_agent_and_mcp_server(stack):
    c, run, pidfile, results = new_run(stack, "long", "long")
    assert api(stack, "POST", f"/api/runs/{run['run_id']}/launch").status_code == 201
    wait_until(lambda: pidfile.exists() and results.exists() and api(stack, "GET", f"/api/runs/{run['run_id']}").json()["agent_process_id"])
    pids = json.loads(pidfile.read_text())
    reg_pid = int(api(stack, "GET", f"/api/runs/{run['run_id']}").json()["agent_process_id"])
    assert reg_pid == pids["agent"] and alive(pids["agent"]) and alive(pids["mcp"])
    t0 = time.time()
    r = api(stack, "POST", f"/api/runs/{run['run_id']}/stop", json={"reason": "e2e stop"}).json()
    assert r["state_committed"] is True and r["run"]["status"] == "stopped"           # state committed first
    wait_until(lambda: not alive(pids["agent"]) and not alive(pids["mcp"]), timeout=15)
    elapsed = time.time() - t0
    wait_until(lambda: not api(stack, "GET", f"/api/runs/{run['run_id']}/launch").json()["alive"])
    st = api(stack, "GET", f"/api/runs/{run['run_id']}/launch").json()
    assert st["escalation"] == []                                                     # supervisor did it; backstop never needed
    assert api(stack, "GET", f"/api/runs/{run['run_id']}").json()["status"] == "stopped"
    print(f"\nstop->agent+mcp gone in {elapsed:.2f}s (supervisor polls every 200ms; HTTP timeout 2s)")
    assert elapsed < 10


@pytest.mark.xfail(strict=False, reason="PERSON 2 DEFECT, INTERMITTENT (apps/mcp-server/lifecycle.py terminate_group): the final os.killpg(SIGKILL) can raise "
                   "PermissionError on macOS (observed once in a manual dashboard run; 6/6 clean in pytest), so the supervisor exits 1 and skips "
                   "its lifecycle event instead of returning 2. Likely cause: the group only holds zombies. Fix: catch "
                   "(ProcessLookupError, PermissionError) around both killpg calls.")
def test_supervisor_exits_cleanly_with_lifecycle_event_after_stop(stack):
    c, run, pidfile, results = new_run(stack, "long", "long2")
    assert api(stack, "POST", f"/api/runs/{run['run_id']}/launch").status_code == 201
    wait_until(lambda: results.exists())
    api(stack, "POST", f"/api/runs/{run['run_id']}/stop", json={})
    wait_until(lambda: not api(stack, "GET", f"/api/runs/{run['run_id']}/launch").json()["alive"])
    st = api(stack, "GET", f"/api/runs/{run['run_id']}/launch").json()
    evs, _ = tool_events(stack, run["run_id"])
    assert st["exit_code"] == 2
    assert any("Agent terminated" in (e["message"] or "") for e in evs)


def test_runner_failure_like_not_logged_in_marks_run_failed(stack):
    c, run, pidfile, results = new_run(stack, "notlogged", "nl")
    assert api(stack, "POST", f"/api/runs/{run['run_id']}/launch").status_code == 201
    wait_until(lambda: api(stack, "GET", f"/api/runs/{run['run_id']}").json()["status"] == "failed")
    got = api(stack, "GET", f"/api/runs/{run['run_id']}").json()
    assert "exited with code 1" in got["stop_reason"]
    assert tool_events(stack, run["run_id"])[1] == []
    log = (stack["tmp"] / "logs" / f"{run['run_id']}.log").read_text()
    assert "Not logged in" in log and "Traceback" not in log                        # failed for the intended reason


@pytest.mark.skipif(os.environ.get("INTENTLOCK_REAL_KIRO") != "1" or not shutil.which("kiro-cli"),
                    reason="REAL Kiro test: needs kiro-cli on PATH, logged in, and INTENTLOCK_REAL_KIRO=1 (NOT run in this environment)")
def test_REAL_kiro_cli_traffic(stack):
    stack["launcher"]._passthrough["KIRO_CLI_BINARY"] = shutil.which("kiro-cli")
    c, run, *_ = new_run(stack, "unused", "real")
    assert api(stack, "POST", f"/api/runs/{run['run_id']}/launch").status_code == 201
    wait_until(lambda: tool_events(stack, run["run_id"])[1], timeout=180)
    _, tools = tool_events(stack, run["run_id"])
    assert all(e["tool"] == "read_file" for e in tools)   # only the IntentLock tool is exposed
