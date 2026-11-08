# TwinGuard local demo

This frozen prototype handles TypeScript/Node patches only. Build the sandbox image once:

```bash
docker build -t intentlock-twinguard:local packages/sandbox-provider
```

`local.py` archives a clean Git base commit, mounts the snapshot and proposed patch read only, and runs the fixed `npm run test:auth` command in a no-network Docker container with a read-only root, dropped capabilities, resource limits, and a disposable writable work directory. It returns a canonical diff and test evidence; it never writes to the original repository. The impact analyzer blocks failing tests, modified trusted tests/config, protected or out-of-contract paths, and exceeded patch limits.

For the demo, create a separate Git repository from `demo/repository` and commit its baseline. Do not run against the IntentLock source repository:

```bash
mkdir -p /tmp/intentlock-demo-repo
cp -R demo/repository/. /tmp/intentlock-demo-repo/
git -C /tmp/intentlock-demo-repo init
git -C /tmp/intentlock-demo-repo add .
git -C /tmp/intentlock-demo-repo -c user.name=IntentLock -c user.email=demo@intentlock.invalid commit -m baseline
git -C /tmp/intentlock-demo-repo rev-parse HEAD
```

Use that HEAD as the contract `base_commit`. Keep `allowed_paths` to `src/auth/**`; keep `tests/auth/**` and `package.json` outside the agent writable scope. Configure the same `INTENTLOCK_ARTIFACT_DIR` for the control plane, execution plane and promoter. Ali's API launcher forwards it only when `INTENTLOCK_AGENT_ENV_PASSTHROUGH=INTENTLOCK_ARTIFACT_DIR` is set. The local artifact directory is a development stand-in for S3.

Start the promoter before launching an agent:

```bash
export INTENTLOCK_API_URL=http://127.0.0.1:8787
export INTENTLOCK_ARTIFACT_DIR=/tmp/intentlock-artifacts
export INTENTLOCK_PROMOTER_URL=http://127.0.0.1:8790
export INTENTLOCK_PROMOTER_TOKEN="$(openssl rand -hex 32)"
mkdir -p "$INTENTLOCK_ARTIFACT_DIR"
python3 packages/patch-promoter/service.py --repository /tmp/intentlock-demo-repo --port 8790
```

The API also needs `INTENTLOCK_INTERNAL_TOKEN`, `INTENTLOCK_PROMOTER_URL`, `INTENTLOCK_PROMOTER_TOKEN`, `INTENTLOCK_REVIEWER_TOKENS`, and its normal launcher configuration. The promoter independently fetches the stored impact report, authenticated review, pending promotion, run and contract, then rechecks the run immediately before `git apply`. Stop, stale base, dirty worktree and changed patch bytes are refused. A passing impact report never promotes automatically.

Run the local verification with an isolated checkout/archive of Ali's branch:

```bash
INTENTLOCK_PERSON1_CHECKOUT=/absolute/path/to/Ali-Person-1 \
  .venv/bin/python -B -m unittest discover -s tests/sandbox -v
```

The actual Docker isolation probe runs with the same flags as patch execution and checks that only loopback is present, outbound TCP is unreachable, and the container root filesystem refuses writes. The full opt-in `test_real_kiro_proposes_reviewed_patch` uses authenticated Kiro to read a file, propose a patch through MCP, execute trusted tests in Docker, submit an impact report, approve it through the local API, and promote the exact patch. It can use local artifacts or live S3. Set `INTENTLOCK_REAL_KIRO=1` and optionally `INTENTLOCK_KIRO_GENERATE_PATCH=1`; the latter asks Kiro to write the diff itself. A browser-driven dashboard recording remains to be made.
