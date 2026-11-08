# Handoff — Person 2 execution plane

## Ali: which branch to use now (read first)

The instructions in this section supersede older branch directions below, which are retained as history. There is no need to merge `codex/ali-review-safeguards` again or pull Person 2's branch into Ali's branch: both are already represented in `integration/main`.

| Branch | What Ali should do |
| --- | --- |
| `Ali-Person-1` | **Make and test the two Ali-owned fixes here:** diagnose the mocked DynamoDB concurrency failure in `apps/api/tests/test_store_contract.py`, and make the event-stream assertion in `tests/integration/test_agent_launch_e2e.py` use a stable event boundary. Commit and push those fixes on this branch. Update `docs/handoffs/person1-control-plane.md` with the cause, fix, and test results. |
| `kavinperson2branch` | Read the latest Person 2 handoff and execution-plane code if needed. Do not commit Ali-owned fixes here. Person 2 owns this branch; its current code is already in `integration/main`. |
| `codex/ali-review-safeguards` | No work needed. Its review-auth and Stop safeguards were already merged into `Ali-Person-1`; this branch is only historical. |
| `integration/main` | After pushing Ali's fixes, fetch `origin/Ali-Person-1` and merge it **into this integration branch**. Run the full combined Python suite with both checkout variables pointing at the integration worktree, plus `npm test` and `npm run build` in `apps/web`. Push `integration/main` only after recording the result; do not make Ali-owned fixes directly on this branch. |
| `main` | Leave unchanged until the integration checks pass and the team reviews the combined branch. Then merge `integration/main` into `main` once. |

Current integration worktree on Kavin's machine: `/Users/kavins/Projects/IntentLock-integration`. Ali can use any clean checkout of `integration/main`; the checkout variables must point to that checkout. The unresolved test details and reproduction commands are in the next section.

## 2026-11-07 integration merge gate — for Ali (current)

Both branches are merged on `integration/main`; `.gitignore` was resolved by keeping both rule sets. `main` remains unchanged. On the integrated worktree, the full Python suite returned **437 passed, 2 failed, 3 skipped, 1 xpassed**. The dashboard's 9 Vitest tests and `npm run build` passed. The two Python failures are in Ali-owned tests and should be resolved or diagnosed before merging `integration/main` into `main`:

1. **Repeatable mocked DynamoDB concurrency failure:** `apps/api/tests/test_store_contract.py::test_event_seq_is_gapless_and_ordered_under_concurrency[dynamodb-moto]` raised `RuntimeError('dictionary changed size during iteration')` from one of 20 concurrent `append_event` calls. It failed again when run alone. The test currently collects worker exceptions without their tracebacks; capture the complete worker traceback first, then determine whether the fault is in Moto's in-memory concurrent implementation or the DynamoStore client path. Do not suppress the exception or weaken gap-free event assertions. If Moto-specific, document that evidence and keep a meaningful concurrency check; verify the production path separately if possible. Reproduce with `.venv/bin/python -B -m pytest -q 'apps/api/tests/test_store_contract.py::test_event_seq_is_gapless_and_ordered_under_concurrency[dynamodb-moto]'` in the integration worktree.
2. **Event-stream timing assertion:** `tests/integration/test_agent_launch_e2e.py::test_launch_api_starts_real_runner_and_scenario_a_is_enforced` read the event list and then opened SSE; a late `agent_event` arrived between those reads, so SSE contained one more event. The test passed on an isolated rerun with `INTENTLOCK_PERSON2_CHECKOUT` set. Make the comparison against a stable event boundary or wait for the runner's final event before taking the list snapshot, while preserving the check that SSE and list deliver the same events. Reproduce with `INTENTLOCK_PERSON2_CHECKOUT=/absolute/path/to/integration/worktree .venv/bin/python -B -m pytest -q tests/integration/test_agent_launch_e2e.py::test_launch_api_starts_real_runner_and_scenario_a_is_enforced`.

After fixing these, rerun the full combined Python suite with `INTENTLOCK_PERSON1_CHECKOUT` and `INTENTLOCK_PERSON2_CHECKOUT` set to the integration worktree; rerun `npm test` and `npm run build` in `apps/web`. Update Ali's own handoff with the result, then merge the integration branch into `main`. Person 2's real Kiro + Docker + S3 workflow passed separately; the combined suite still skips its explicit live-Kiro case unless `INTENTLOCK_REAL_KIRO=1` is set. A browser-driven dashboard recording remains open.


## 2026-11-07 completion and live workflow verification (latest)

Branch: `kavinperson2branch`. The Kiro supervisor now handles SIGTERM and macOS `os.killpg` permission failures, reports clean exit as `completed` and failure or interruption as `failed` through Ali's `/internal/runs/{run_id}/state`, and preserves a prior Stop/blocked status. The MCP launcher forwards only configured artifact environment values; this fixed an unset S3 bucket placeholder that broke a real Kiro patch proposal. MCP preview errors now return bounded reason codes. TwinGuard's trusted test and test-script changes produce `TRUSTED_TEST_MODIFIED`; its real Docker isolation probe verifies no outbound network, loopback-only interfaces, and a read-only root with the same flags used by patch execution.

Verification: 32 Person 2 tests passed against an isolated Ali API checkout, with one explicit real-Kiro opt-in skipped in the default suite. The tests include SIGTERM child cleanup, exact run-completion events, stopped-run precedence, trusted-test blocking, Docker isolation, and actual local HTTP promotion. Separately, the opt-in workflow **passed with authenticated Kiro generating its own patch and live S3 evidence**: Kiro read through MCP, proposed through MCP, TwinGuard ran Docker and trusted tests, the impact was approved through Ali's API, and the exact approved patch was promoted to a temporary Git repository. The same workflow also passed with local artifact storage. This was an API-driven review and promotion, not a browser-click recording. The earlier standalone Kiro smoke verified an allowed read and denied `.env` read; the full patch workflow exercises an allowed read and patch proposal.

For Ali: pull this branch for integrated testing. Use a clean demo Git repository and the same S3 bucket/profile (or artifact directory) in the API, Kiro MCP environment, and promoter; keep reviewer and promoter tokens local. Check dashboard launch and Stop manually, then capture the browser demonstration. The Docker image must be built locally. Kiro itself is outside the container, and the fallback after macOS group-signal denial cannot guarantee descendant cleanup. The local run-state endpoint provides authoritative completion; do not infer completion from a detached PID or UI timeout. AgentCore remains unimplemented.


## 2026-11-07 S3 evidence update (latest)

Branch: `kavinperson2branch`. `packages/sandbox-provider/artifacts.py` now provides immutable local/S3 repository input, patch, test, impact, and final audit evidence with key validation, bounded sizes, S3 conditional writes, SSE-S3, SHA-256 checksums, and read-time hash verification. The MCP preview and patch promoter use the same store interface. Multiple candidates in a run use distinct test/impact keys. `packages/sandbox-provider/export_audit.py` uploads the terminal run's control-plane timeline. `apps/mcp-server/start_api_with_s3.py` runs Ali's unmodified API with the S3 reader injected through its existing factory parameter. The local backend remains available.

**Live AWS:** stack `intentlock-execution-plane-dev` was deployed to account `247495661854` in `us-east-1` from `infrastructure/execution-plane/template.yaml`. Bucket `intentlock-evidence-247495661854-us-east-1` is private, SSE-S3 encrypted, versioned, TLS-only, and retained; its four public-access blocks were verified. Two scoped IAM managed policies were created but are not attached to a principal. Real S3 put/read through `S3Artifacts` passed. A real S3 end-to-end test passed with Docker + Ali's local API: unsafe patch blocked, Stop prevented promotion, safe patch reviewed and exact bytes promoted. Input archive, patch, test, impact and final audit objects are visible in the bucket. All 25 Person 2 local tests passed. See `infrastructure/execution-plane/README.md` for commands and current limits.

**For Ali:** the normal API factory still selects `LocalDirArtifactReader` by default. On integration, select `S3Artifacts` when `INTENTLOCK_ARTIFACT_BUCKET` is set, or use Person 2's `start_api_with_s3.py` adapter for the local demo. The API's `artifacts` parameter already accepts the reader interface; this was verified in the real S3 HTTP test. Keep the API and promoter on localhost until public authentication exists. The launcher must pass `INTENTLOCK_ARTIFACT_BUCKET,INTENTLOCK_AWS_PROFILE` through its explicit env allowlist. Do not pass AWS credentials into Docker. The local Kiro process still inherits HOME, so the demo does not establish OS-level isolation from local AWS credentials.

**Still open:** use an isolated least-privilege role/credential instead of the current admin user; record the manual dashboard-to-promotion flow. No AgentCore resources were created. The final audit export is explicit after the run reaches a terminal state; the dashboard does not trigger it automatically.

## 2026-11-07 TwinGuard update (supersedes older status below)

Branch: `kavinperson2branch`. The latest change adds a working local TypeScript/Node TwinGuard path: `@intentlock/propose_patch` authorizes through Ali's `/internal/authorize`, runs a patch in a read-only-root, no-network Docker container, executes the fixed trusted `npm run test:auth`, analyzes paths/limits/tests, stores SHA-256-addressed patch evidence in a local artifact directory, and sends an ImpactReport to `/internal/impact`. The protected Git repository remains clean during previews. `packages/patch-promoter/service.py` serves localhost `/promote`, independently verifies the stored impact, human approval, pending promotion, eligible run, active contract, base commit, and artifact hash; it rechecks run status immediately before applying the exact patch. Stop blocks promotion.

Verification: three real Docker sandbox tests passed, including unsafe auth bypass blocked and safe timeout fix promoted. One real HTTP end-to-end test against an isolated archive of `origin/Ali-Person-1@3105520` passed: blocked impact could not be approved; Stop prevented a previously approved promotion; a fresh run's passing impact was approved and promoted through the real HTTP promoter. The existing two cross-workstream HTTP tests passed. The opt-in **real Kiro** launcher test passed (one tool call observed) after correcting its test fixture to pass through the user's actual logged-in `HOME`; that fixture change was made only in a temporary archive, not in Ali's branch. `kiro-cli whoami` outside the sandbox confirmed Google login. Ali's unmodified fixture with a temporary `HOME` times out because Kiro appears logged out. An unrelated Ali SSE test in the broader `-k REAL` run observed a late event after its initial list read; this appears to be an assertion race, not a TwinGuard failure. The single dashboard → real Kiro → unsafe/safe patch → human review → promotion sequence has **not** been manually recorded yet; the Kiro and TwinGuard legs were verified separately with the actual control plane.

Local demo setup: `packages/sandbox-provider/README.md`. Ali's API must set `INTENTLOCK_AGENT_ENV_PASSTHROUGH=INTENTLOCK_ARTIFACT_DIR` and pass the same local artifact root to itself, the Kiro launcher, and promoter. Build `intentlock-twinguard:local`; create and commit a **separate** demo Git repository from `demo/repository`; set the contract base commit to its HEAD and restrict writable paths to `src/auth/**`. Configure reviewer and promoter tokens locally; no secrets in Git. The local artifact directory is a development stand-in for S3. No real AWS resources are used by TwinGuard yet.

For Ali: integrate this branch with his current branch, then run the complete dashboard demo with the shared artifact root and real logged-in Kiro. Update the opt-in Kiro test fixture so its real-Kiro case passes through the authenticated `HOME`. Keep the trusted tests and `package.json` outside agent writable paths. Review whether the execution-plane `propose_patch` MCP tool should be enabled in the production Kiro profile only after Docker and artifact storage are configured. Current promoter recheck narrows the Stop race; a fully atomic Stop-versus-apply guarantee would require a shared lock or transactional promotion coordinator across planes.

Open: real AWS S3/AgentCore integration; full browser demonstration capture; interactive Kiro tool exposure inspection; production reviewer authentication. The Docker image is local only. No claim that the entire Kiro sandbox is OS-contained: Kiro itself runs outside Docker and has a constrained profile.

Updated: 2026-11-07. Branch: `kavinperson2branch`.
Commit: use `git log -1 -- docs/handoffs/person2-execution-plane.md` after this milestone commit.
Scope: Kiro CLI coding agents for security/platform/DevOps teams; broader agents deferred.

## Completed

- Read-only stdio MCP adapter (`apps/mcp-server/server.py`), bounded UTF-8 reads,
  path traversal/symlink/protected-path defenses, authorization before filesystem access.
- HTTP integration with Person 1: `INTENTLOCK_API_URL`, `INTENTLOCK_INTERNAL_TOKEN`,
  `X-IntentLock-Internal-Token` header. Reject redirects, non-local plaintext HTTP,
  malformed schemas, inconsistent allows and mismatched request IDs.
- Kiro 3.1 profile/launcher: only IntentLock read tool, clean temporary working dir.
- Process supervisor registers PID at `/internal/runs/{id}/agent`, polls `/api/runs/{id}`,
  terminates process group on terminal/unavailable state, emits sanitized lifecycle event.
- SandboxProvider interface and TypeScript demo bug fixture (not sandbox implementation).

## Schema and ownership decisions

Accept all six schema 0.1.0 deviations listed in Person 1 handoff §8 for this prototype.
No shared schemas or Person 1 source files were changed. Owned schema snapshots mirror
his canonical request/decision/run files for independent branch operation. Refresh in
coordination; canonical authority remains `packages/contracts` after integration.

Use authenticated HTTP, not direct Lambda invocation. Person 2 will emit impact and
promotion events after implementing them; Person 1 owns authorization/run events.
`pass` never authorizes automatic promotion. Permission-request and finish tools are
not exposed yet. AgentCore interceptor mapping is deferred until Gateway integration.

## Verified

15 tests passed, including 2 real localhost HTTP cross-workstream tests and 12
parameterized subtests. Integration used an isolated archive of `origin/Ali-Person-1`:
MCP subprocess → actual API → ALLOW, PROTECTED_PATH, RUN_NOT_ACTIVE; checked events
contain decisions without file contents. Stop test uses a real sleeping subprocess
and verifies it is gone after API stop (not a live Kiro model session).

Actual Kiro 3.1 smoke passed against the local API from `origin/Ali-Person-1` at
`1bfd0ff`: authenticated Kiro made a real MCP `read_file` call (ALLOW), then a
second call for `.env` was denied (PROTECTED_PATH). The blocked run terminated
the launcher with exit code 2. The launcher now explicitly selects Kiro V3;
without `--v3`, its custom agent was not found.

## Known limits / next steps

- Authenticated Kiro MCP traffic is verified; inspect full tool exposure in an interactive session before claiming all possible Kiro data access is mediated.
- Docker absent; no sandbox, patch inspection or promotion implemented yet.
- No AgentCore/S3 or AWS deployment yet.
- Read is the only exposed tool. List/search need per-result policy filtering.
- Polling has 200 ms spacing and 2-second HTTP timeout, so no sub-second SLA claimed.
- Local process timeout kills the agent and emits an event. Ali has since added
  `/internal/runs/{run_id}/state`; connect the runner to it during integration.
- Trusted regression tests/test command configuration must be outside writable scope.
- Kiro configuration is not OS containment; review global auto-loaded context before
  claiming all repository access is mediated.

See `apps/mcp-server/README.md` for launch and test commands. Person 1 can merge/cherry-pick
this branch after review; no control-plane files are included in this commit.

## For Ali: next control-plane steps

1. Review this branch against `Ali-Person-1`. Keep the canonical 0.1.0 schemas in
   `packages/contracts`; the three copies under `apps/mcp-server/schemas` are only a
   temporary branch bridge. After integration, replace those copies with canonical
   schema loading and run the cross-workstream tests.
2. Ali has added `/internal/runs/{run_id}/state` for completion/failure. Person 2
   should use it when the runner finishes; preserve stop-before-success ordering.
3. Ali has wired the dashboard launch action to a runner using the returned
   `run_id`, `contract_id`, and `contract_version`. Verify the combined flow; the runner registers its PID through
   `/internal/runs/{run_id}/agent`; the UI's stop action uses the existing API endpoint.
4. Keep `/internal/authorize` as the deterministic authority for each MCP call, with
   `X-IntentLock-Internal-Token`. Keep authorization and event writes serialized or
   atomic when replacing the in-memory store with DynamoDB.
5. The live Kiro smoke now passes. Integrate the branch and repeat it against the
   combined application. Inspect the active run feed for real MCP requests and
   verify that no built-in file/shell tools are available in the Kiro profile.
6. Agree on the contract's trusted test catalogue before mutation work: agent-editable
   `package.json` or `tests/auth/**` must not be able to weaken the regression suite.

Person 2's next work is the container sandbox, patch impact analysis, exact promotion,
and later AgentCore integration. Those are not implemented in this branch yet.

## Immediate handoff for Ali — review safeguards and current Kiro branch

This section supersedes the older "For Ali" action list where Ali has already
implemented run-state, launch and trusted-test work. The safeguard fix is on
`codex/ali-review-safeguards` (commit `8026498`), branched from Ali's `a92b199`.
It changes Ali-owned control-plane files. Keep Person 2 work on `kavinperson2branch`.

1. Review `codex/ali-review-safeguards` against `Ali-Person-1`, then merge it into
   `Ali-Person-1` after review. It requires a reviewer-specific token for approve,
   reject and promote, and blocks approval/promotion after a run is stopped.
2. Set `INTENTLOCK_REVIEWER_TOKENS` locally to a JSON map of reviewer names to
   private random tokens. Verify a matching reviewer/token pair can approve, and
   missing, wrong and another reviewer's tokens return 401. The dashboard asks for
   a token at review time; do not place one in `VITE_*` variables or commit it.
3. Verify Stop blocks both a fresh approval and promotion after approval. Rerun
   the impact API tests and dashboard build. The fix branch passed 62 impact API
   tests and the dashboard build before handoff. An in-flight promotion still
   requires the execution-plane promoter to recheck run state before applying.
4. Fetch the latest `kavinperson2branch` and rerun the combined Kiro integration
   and dashboard launch flow. The older Ali handoff tested an outdated Person 2
   branch. Real Kiro 3.1 MCP traffic has now been observed on Person 2's machine:
   one ALLOW for `src/auth/session.ts`, then PROTECTED_PATH for `.env`.
5. Update `docs/handoffs/person1-control-plane.md` with the current branch name,
   integration results and remaining limits. Once the safeguard branch is merged,
   delete that temporary branch if it is no longer needed.
