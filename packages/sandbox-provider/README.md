# TwinGuard local demo

This frozen prototype handles TypeScript/Node patches only. Build the sandbox image once:

```bash
docker build -t intentlock-twinguard:local packages/sandbox-provider
```

`local.py` archives a clean Git base commit, mounts the snapshot and proposed patch read only, and runs the commands chosen from the trusted check catalogue (`packages/impact-analyzer/catalogue.json`; the original contract-approved `npm run test:auth` is one entry) in a no-network Docker container with a read-only root, dropped capabilities, resource limits, and a disposable writable work directory. It returns a canonical diff and test evidence; it never writes to the original repository. The impact analyzer blocks failing tests, modified trusted tests/config, protected or out-of-contract paths, and exceeded patch limits.

A `package.json` edit that changes only dependency fields runs the same trusted checks and returns `review_required` with a `DEPENDENCY_CHANGE` finding. Changes to scripts or other package fields still produce `TRUSTED_TEST_MODIFIED` and never execute the changed script. The promoter accepts `review_required` only after the normal authenticated review and exact-patch checks.

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

## Check catalogue and selection

`catalogue.json` is administrator-owned and loaded by host-side code only (override the path with `INTENTLOCK_CHECK_CATALOGUE`); it is never in the agent workspace. Each check has an ID, `kind` (`test` or `static`), argv, path globs it covers, and a timeout. `execute(..., checks=[...])` writes the selected checks to a read-only `/patch/checks.txt` and `runner.sh` runs exactly those under `timeout`, writing per-check output, exit code and duration. `run_check()` runs one check on the clean base snapshot for `run_approved_command`. The image pins `typescript@5.6.3` at build time (`docker build -t intentlock-twinguard:local packages/sandbox-provider`), so type-checking needs no network at run time.
