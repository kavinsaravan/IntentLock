# Cross-workstream integration tests and the real-Kiro procedure

## What each kind of test proves

| Kind | Where | Real | Synthetic / simulated |
|---|---|---|---|
| Control-plane unit/API tests | `apps/api/tests`, `tests/policy`, `tests/contract` | policy engine, API, store logic | DynamoDB is `moto` (mocked AWS); launcher tests use stand-in scripts; impact data is built from fixtures |
| Launch e2e (`test_agent_launch_e2e.py`) | needs `INTENTLOCK_PERSON2_CHECKOUT` | my API + launcher; Person 2's launcher, supervisor and MCP server; real processes/HTTP/SSE | the agent's brain: a scripted `kiro-cli` stand-in (no LLM) |
| Real Kiro (`test_REAL_kiro_cli_traffic`) | same dir, opt-in | everything incl. Kiro | nothing — but it has NOT been run by Person 1 (Kiro is not installed on this machine) |
| Person 2's own suite | their branch `tests/sandbox` with `INTENTLOCK_PERSON1_CHECKOUT` | their MCP→my API over localhost HTTP | their stop test uses a sleeping subprocess |

Person 2 independently reported a REAL authenticated Kiro 3.1 run against commit `1bfd0ff` (ALLOW on a real `read_file`, then `PROTECTED_PATH` on `.env`, launcher exit 2). That predates the dashboard launcher; the combined system has not been run with real Kiro yet.

## Procedure to run the combined system with real Kiro (Kavin's machine)

Prerequisites: checkouts of `Ali-Person-1` (control plane) and `kavinperson2branch` (execution plane) in two separate directories; Python 3.11+; Node 18+; `kiro-cli` installed and logged in (`kiro-cli login`), Kiro 3.x.

```bash
# 1. control plane venv (in the Ali-Person-1 checkout)
python3 -m venv .venv && .venv/bin/pip install -r apps/api/requirements-dev.txt

# 2. start the API with the launcher pointed at Person 2's runner (absolute paths!)
P1=$PWD; P2=/abs/path/to/kavinperson2branch-checkout
export INTENTLOCK_INTERNAL_TOKEN=$(openssl rand -hex 16)
export INTENTLOCK_API_URL=http://127.0.0.1:8787
export INTENTLOCK_AGENT_LAUNCH_ARGV=$(python3 -c "import json,sys;print(json.dumps(['$P1/.venv/bin/python','-B','$P2/apps/mcp-server/launch_kiro.py','--repository','$P2/demo/repository','--run-id','{run_id}','--contract-id','{contract_id}','--contract-version','{contract_version}','--prompt={mission}']))")
# only if kiro-cli is not on PATH:  export KIRO_CLI_BINARY=/abs/path/to/kiro-cli
cd apps/api && PYTHONPATH=$P1/packages/policy-engine:. $P1/.venv/bin/python -m uvicorn intentlock_api.app:create_app --factory --port 8787

# 3. dashboard (second terminal, Ali-Person-1 checkout)
cd apps/web && npm install && npm run dev      # http://localhost:5173
```

Run it in the dashboard: Create mission (base commit = any 40-hex; "Use fixture draft" if Bedrock is not configured) → Approve and launch.

Expected, and what to capture as evidence (screenshot + `curl -s localhost:8787/api/runs/RUN_ID/events`):
1. Active Run shows `Agent launcher: RUNNING`, an `Agent process` PID registered by Person 2's supervisor, and the run `ACTIVE`.
2. The live feed shows REAL `read_file` requests from Kiro with `ALLOW`/`DENIED` decisions. Only `read_file` is exposed today. If Kiro tries `.env`: `PROTECTED_PATH`, the run turns `BLOCKED`, the launcher exits (Person 2 observed code 2).
3. Launch a second run and press **Stop run**: the page shows `STOPPED` immediately; then verify no leftovers: `ps aux | grep -E "kiro|server.py|launch_kiro"` shows nothing within a few seconds. Record the time.
4. A request after stop must show `RUN_NOT_ACTIVE`.

Automated version (needs the same prerequisites):
```bash
INTENTLOCK_REAL_KIRO=1 INTENTLOCK_PERSON2_CHECKOUT=$P2 .venv/bin/python -m pytest tests/integration -k REAL -q -s
```

### Tool-exposure inspection (required before anyone claims all Kiro access is mediated)
Person 2's profile (`launch_kiro.py`) lists `@intentlock/read_file` only and denies `builtin` capabilities with `includeMcpJson/includePowers: false`. That is configuration, not proof. In an interactive session started the same way (`kiro-cli chat --v3 --agent intentlock` from the temp workspace the launcher creates), list the agent's available tools and ask it to (a) read a file with any built-in file tool, (b) run a shell command, (c) fetch a URL. Record that each is unavailable/denied. Also review Kiro's global auto-loaded context/steering files. Until that is recorded, treat tool access as unverified.
