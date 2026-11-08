"""Record a real Security Replay Scenario A run: an authorized read followed by a denied protected-file read.

Everything that decides is real: this control-plane API (in-process, in-memory store), the deterministic policy
engine, Person 2's MCP stdio server (apps/mcp-server/server.py) and audit exporter
(packages/sandbox-provider/export_audit.py). The agent is a scripted MCP client, not Kiro: it sends the same
`tools/call` messages Kiro would. No Docker, Kiro, AWS or Bedrock is used.

Usage (project venv):
  .venv/bin/python apps/api/record_replay_scenario_a.py --artifact-dir /path/to/artifacts
Then start the API with INTENTLOCK_ARTIFACT_DIR=/path/to/artifacts and open #/replay in the dashboard.
"""
import argparse
import importlib.util
import json
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import uvicorn

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "packages/policy-engine"), str(ROOT / "apps/api")]

from intentlock_api.app import create_app  # noqa: E402
from intentlock_api.artifacts import LocalDirArtifactReader  # noqa: E402
from intentlock_api.store import LocalMemoryStore  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def record(artifact_dir: pathlib.Path, approved_by: str) -> dict:
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(24)
    with tempfile.TemporaryDirectory(prefix="intentlock-scenario-a.") as tmp:
        repo = pathlib.Path(tmp) / "repository"
        shutil.copytree(ROOT / "demo/repository", repo)
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=IntentLock", "-c", "user.email=demo@intentlock.invalid", "commit", "-qm", "baseline")
        base = _git(repo, "rev-parse", "HEAD")

        app = create_app(LocalMemoryStore(), internal_token=token, artifacts=LocalDirArtifactReader(str(artifact_dir)),
                         promoter=None, reviewers=None, reviewer_tokens={}, replay_sources=[])
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(url + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        try:
            draft = json.loads((ROOT / "packages/contracts/fixtures/contract-draft.json").read_text())
            draft["base_commit"] = base
            contract = httpx.post(url + "/api/contracts", json={"draft": draft, "approved": True,
                                                                "approved_by": approved_by}).json()
            run = httpx.post(url + "/api/runs", json={"contract_id": contract["contract_id"],
                                                      "contract_version": contract["version"]}).json()
            env = {"PATH": "/usr/bin:/bin", "INTENTLOCK_API_URL": url, "INTENTLOCK_INTERNAL_TOKEN": token}
            mcp = subprocess.Popen([sys.executable, "-B", str(ROOT / "apps/mcp-server/server.py"), "--repository", str(repo),
                                    "--run-id", run["run_id"], "--contract-id", contract["contract_id"],
                                    "--contract-version", str(contract["version"])],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

            def call(i, method, params=None):
                mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                mcp.stdin.flush()
                return json.loads(mcp.stdout.readline())

            call(0, "initialize")
            allowed = call(1, "tools/call", {"name": "read_file", "arguments": {"path": "src/auth/session.ts"}})["result"]
            denied = call(2, "tools/call", {"name": "read_file", "arguments": {"path": ".env"}})["result"]
            mcp.stdin.close()
            mcp.wait(timeout=10)
            final = httpx.get(f"{url}/api/runs/{run['run_id']}").json()
            if allowed["isError"] or not denied["isError"] or final["status"] != "blocked":
                raise SystemExit(f"Scenario A did not behave as expected: allowed={allowed['isError']} "
                                 f"denied={denied['content'][0]['text']} run={final['status']}")

            exporter = _load("intentlock_export_audit", ROOT / "packages/sandbox-provider/export_audit.py")
            key, digest = exporter.export(exporter.Client(url, token), exporter.artifacts.LocalArtifacts(str(artifact_dir)),
                                          run["run_id"])
            return {"run_id": run["run_id"], "run_status": final["status"], "stop_reason": final["stop_reason"],
                    "denied_reason": denied["content"][0]["text"], "audit_key": key, "audit_sha256": digest,
                    "artifact_dir": str(artifact_dir)}
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact-dir", required=True, type=pathlib.Path)
    parser.add_argument("--approved-by", default="ali")
    print(json.dumps(record(parser.parse_args().artifact_dir.resolve(), parser.parse_args().approved_by), indent=2))
