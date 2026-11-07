# Person 2 execution plane

Scope: Kiro CLI coding agents for security/platform/DevOps teams. Python 3.11+.
Install dependencies with `python3 -m pip install -r apps/mcp-server/requirements.txt`.

Implemented: stdio MCP initialization/discovery/ping and authorized bounded UTF-8
`read_file`; schema-validated HTTP authorization; Kiro process registration and
polling of authoritative run state; graceful process-group stop and hard-kill fallback.
Missing/malformed authorization fails closed. No mutation tools are exposed yet.

## Launch

Set `INTENTLOCK_API_URL` (e.g. `http://127.0.0.1:8787`) and
`INTENTLOCK_INTERNAL_TOKEN` to match Person 1's API. HTTP is restricted to localhost;
remote connections require HTTPS. Redirects are rejected. The client uses
`X-IntentLock-Internal-Token`, never a Bearer header.

Create an approved contract and active run through the control plane first, then:

```sh
python3 -B apps/mcp-server/launch_kiro.py --repository demo/repository --run-id RUN_ID --contract-id CONTRACT_ID --contract-version 1
```

`--print-profile` inspects the config without launching. `KIRO_CLI_BINARY` optionally
specifies an absolute binary path. Authenticate Kiro with `kiro-cli login` first.
The launcher uses a fresh temporary directory, only the IntentLock MCP read tool,
no inherited MCP configuration/powers, and denied built-in capabilities. See
https://kiro.dev/docs/custom-agents/configuration-reference/.

The supervisor registers Kiro's PID, checks run identity/state every 200 ms between
HTTP calls, and terminates the process group on a terminal state, unavailable or
malformed run response, interruption, or a 600-second local timeout. HTTP timeout
is two seconds: this is not a guaranteed sub-second stop SLA. Each tool separately
reauthorizes, preserving immediate denial after the control plane commits stop.

## Tests

```sh
python3 -B -m pytest -q tests/sandbox
INTENTLOCK_PERSON1_CHECKOUT=/path/to/isolated/ali-checkout python3 -B -m pytest -q tests/sandbox
```

Integration tests run Ali's actual FastAPI app on an ephemeral localhost port,
create test contracts/runs, exercise the MCP subprocess and check process shutdown.
Without the checkout variable, the two cross-workstream tests are explicitly skipped.
Install Ali's API dependencies in your Python environment for integration tests.

Opt-in live test (authenticated Kiro and model access required):

```sh
INTENTLOCK_PERSON1_CHECKOUT=/path/to/isolated/ali-checkout python3 -B tests/sandbox/kiro_smoke.py
```

The synthetic prompt explicitly asks for an allowed read and a protected read; it
tests routing, not whether a model is susceptible to a prompt-injection payload.

## Shared schema agreement

Accept Person 1's proposed schema 0.1.0 wire formats and six deviations in his handoff.
`pass` still requires human patch approval. `request_permission`/`finish_task` are
not exposed by this increment. JSON schemas in `schemas/` are unmodified snapshots
from `origin/Ali-Person-1`; the canonical files remain `packages/contracts/schemas`.
Coordinate updates with Ali and refresh these snapshots together. Do not edit them
independently. Tests validate real request/decision/run payloads against them.

## Remaining work

Authenticated Kiro end-to-end verification; list/search with per-result filtering;
container sandbox implementation; trusted tests and patch fixtures; impact analysis;
reviewed patch promotion; S3 artifacts; AgentCore Gateway and Code Interpreter.
The provider file is an interface only, Docker is unavailable, and no AWS deployment
has been performed. The demo repository is a source fixture, not a clean standalone
Git snapshot. Public API authentication remains Person 1's known demo limitation.
