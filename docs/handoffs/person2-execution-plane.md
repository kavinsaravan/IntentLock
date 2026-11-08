# Handoff — Person 2 execution plane

## 2026-11-07 judge demo preparation (latest)

The root README now points to `docs/demo-runbook.md`; `demo/start_local_demo.sh` preflights Docker, authenticated Kiro, Python/web dependencies, and API token configuration, then creates a fresh temporary Git repository and starts the local promoter, API, and Vite dashboard. The operator runbook covers a five-minute judge walkthrough, recording, Bedrock fixture fallback, and honest AWS/AgentCore claims. A dry run passed: the API returned healthy local-memory state, `/api/launcher` reported `configured: true`, the dashboard served HTTP 200, and the stack stopped cleanly. The full browser/Kiro unsafe-then-safe run remains evidenced by `docs/demo-evidence/`; it was not re-recorded in this preparation pass. Before presenting, rehearse the script with the actual reviewer token and confirm the live Bedrock proposal if it will be shown.

## 2026-11-07 rejected-patch final audit

The control plane's authenticated rejection endpoint now asks the local promoter to finalize the run audit. The promoter accepts this request only with its service token, re-reads the authoritative impact and review state, and writes `runs/{run_id}/audit/final.json` through the configured immutable local/S3 artifact store after the run is terminal. Repeating the same rejection retries finalization. A still-active run queues finalization until it becomes terminal. The supervisor now emits its last agent event before reporting terminal state, so the finalizer cannot observe completion before that event is recorded. No public API authentication, IAM, CloudWatch, hosting, or AWS deployment changes were made; Kavin deferred that production-hardening work for the hackathon.

Verification: real local HTTP API + Docker tests covered rejection after completion, rejection before completion, missing promoter token, and an unreviewed report. `test_twinguard_api.py`: **6 passed, 1 real-Kiro opt-in skipped**. Supervisor integration: **6 passed**. API impact/reviewer authentication tests: **114 passed**. The prior real authenticated Kiro patch workflow passed before this change; it was not rerun for this rejection-only path. The API and promoter must share a configured artifact store for automatic export; when the promoter is unavailable, the review stays recorded and the response reports `audit_status: unavailable` so the same rejection can be retried.

## 2026-11-07 combined demo verification and AWS status

Person 2 merged `origin/main@de1d546` into `kavinperson2branch` at `26cd5fe`, retaining all of Ali's combined control-plane code and the prior Person 2 work. The pre-existing staged `AGENTS.md` was stashed before the merge because Git refused to overwrite it; the merge brought in the exact same blob from main, and the backup stash remains available.

**Verified local product flow:** one headless Chrome session launched real authenticated Kiro from the dashboard for `run_f7211e0c76a8`. Kiro read through MCP and proposed an auth-bypass patch and a corrected timeout patch against the same unchanged base. Docker TwinGuard blocked the first with `CRITICAL_TEST_FAILURE`; the dashboard displayed the earlier report and disabled approval. The reviewer selected the later passing report, approved its exact digest, and requested promotion. The local promoter applied only that correction, and `audit/final.json` contained the promotion event. Screenshots and limits of this evidence are in `docs/demo-evidence/README.md`. This run used local memory and local artifacts, so it is not evidence of a deployed AgentCore or DynamoDB dashboard path.

**Bedrock:** a live `us.anthropic.claude-haiku-5-5` Converse call initially rejected the deprecated `temperature` parameter and returned reasoning before text. Both were fixed. The proposal validator now retries drafts that do not use the frozen `auth-tests` → `npm run test:auth` catalog or omit `verifyToken`; one later live proposal passed on attempt 2. Headless Chrome then clicked **Generate contract** and verified that the dashboard displayed a real Bedrock draft with the executable Node test catalog. The Bedrock screenshot was refreshed after the fix.

**AWS evidence:** a synthetic permission note was written and read successfully in the existing private S3 evidence bucket under the new agent-evidence key pattern. With Kavin's explicit approval, the rebuilt authorizer zip was deployed to the existing `intentlock-authorizer` Lambda in account `247495661854`, `us-east-1`; local zip and Lambda code SHA-256 matched (`x/EuxAFygH/B92Au4sct6d/y7C1IZSmH0nxK0SDNvyE=`). A live empty request returned `deny / INVALID_ARGUMENTS`. The latest CloudWatch log contained a redacted EMF record with `AuthorizationLatencyMs`, `ToolRequestsAllowed`, and `ToolRequestsDenied`; CloudWatch `list-metrics` showed `AuthorizationLatencyMs` in namespace `IntentLock`.

**Regression:** combined Python suite **437 passed, 16 skipped**; dashboard **9 Vitest tests passed** and production build succeeded. Focused Bedrock and CloudWatch tests passed after the combined suite, and the full browser/Kiro flow passed. The `PATCH_DOES_NOT_APPLY` distinction and Git `--recount` helped Kiro correct malformed patch hunks without relaxing path, test, or promotion checks. The protected repository stayed unchanged until exact-hash promotion.

**Still open:** AgentCore Gateway/REQUEST interceptor and Code Interpreter are not deployed. PRD §14.2/§23 explicitly permits the honest local MCP compatibility proxy and locked-down Docker fallback; Code Interpreter VPC mode with no public route and an AWS-reachable Gateway target would need separate infrastructure. Other CloudWatch metrics in PRD §14.7, public API authentication, narrow runtime IAM roles, and a deployed dashboard/API path remain. A completed run whose patch is rejected after the agent exits still needs a manual final-audit export because Ali's review endpoint has no finalizer callback. The single browser run above used local memory/artifacts; earlier separate tests verified DynamoDB/Lambda and S3. Do not describe the fallback as AgentCore or claim production readiness.

## 2026-11-07 MCP coverage and audit automation

The Kiro MCP adapter now implements all seven frozen PRD tools. `list_files` and `search_code` filter results against the active contract and skip protected paths; `run_approved_test` uses a fresh Docker snapshot and the fixed auth-test command. Permission requests and task summaries become immutable S3/local evidence and do not grant new access. The supervisor exports `audit/final.json` on terminal runs without pending review. For a successful patch, it waits until the promoter sees the API's recorded human promotion before exporting, so the immutable audit includes promotion. The full Person 2 suite passed before the final audit timing fix (**38 tests, one skipped**); focused artifact, HTTP, Docker, and real authenticated Kiro tests passed after it. The real Kiro test verified the final audit contains the promotion event. A completed run whose patch is later rejected still needs a manual audit export; there is no callback from Ali's review endpoint yet. AWS Gateway and Code Interpreter remain separate, unimplemented integration work.

## 2026-11-07 dependency review rule (newest Person 2 change)

TwinGuard now distinguishes dependency-only `package.json` edits from changes to test scripts or other package fields. Dependency-only edits run the fixed `npm run test:auth`, produce a medium `DEPENDENCY_CHANGE` finding and `review_required` decision, and remain subject to authenticated human review, run/base/hash checks, and exact promotion. Test scripts/config remain critical `TRUSTED_TEST_MODIFIED` blocks and are never executed after modification. This aligns the shared `benign-dependency-bump` vector without weakening trusted-test protection. The full Person 2 suite passed against Ali's combined API: **34 tests, one skipped** (real Kiro opt-in). This includes a real Docker dependency test and a local HTTP API/promoter dependency-promotion test. No AWS resource changed in this milestone.

## 2026-11-07 post-merge verification of `main` (latest)

`origin/main@de1d546` is a merge of `integration/main@a830c60`; their file trees were compared and are identical. Kavin verified the exact merged commit in a clean temporary worktree, rebuilt `intentlock-twinguard:local` from it, and used the integration virtual environment only for test dependencies. **All checks passed:** six real Docker tests (including no-network/read-only-root isolation, trusted-test blocking, unsafe patch blocking, Stop/tamper refusal, and reviewed safe promotion); one HTTP TwinGuard test with real S3 evidence (unsafe auth bypass blocked, corrected timeout patch promoted, final audit exported); and one authenticated real Kiro test with `INTENTLOCK_KIRO_GENERATE_PATCH=1` and S3 enabled (Kiro read and proposed through MCP, Docker ran trusted tests, the API recorded review, and the exact patch was promoted). Kiro was logged in; the S3 bucket was reachable.

S3 object listing after the runs confirmed input archive, patch, test output, and impact objects for the real Kiro run under `runs/run_a488f91bff62/`; the corrected-patch test also wrote `runs/run_95c5e25ba191/audit/final.json`. The Kiro opt-in test does not export a final audit automatically. These checks used the local in-memory API and local HTTP promoter with real S3 artifact storage; they do not claim that the deployed Lambda/DynamoDB path or dashboard launched that same Kiro run. The earlier full combined Python suite passed on the identical integration file tree (439 passed, 3 skipped, 1 xpassed), and the dashboard tests/build passed there. No new code issue was found in the merged execution path.

Follow-up: a single browser-driven dashboard launch → real Kiro → unsafe/corrected patch → review → promotion recording is still outstanding. Bedrock AgentCore is not implemented; Bedrock proposal has not had a live model invocation. Keep the current demo claims limited to the verified paths. This verification updates Person 2's handoff on `kavinperson2branch`; it does not change `main` code.

## 2026-11-07 latest integration verification — read first

Ali's fixes (`074210c`) are merged into `integration/main@bcb40b7`. Kavin fast-forwarded the clean integration worktree to that commit and confirmed that it contains Person 2's supervisor fixes: SIGTERM handling, `os.killpg` permission fallback, and terminal run-state reporting. The real authenticated Kiro test then passed: Kiro generated a patch through MCP, TwinGuard executed it in Docker, and an API-driven human review promoted the exact patch. The full combined Python suite passed: **439 passed, 3 skipped, 1 xpassed**. The dashboard passed **9 Vitest tests** and `npm run build`.

A separate headless Chrome dashboard check used the integrated API, real Docker previews, and the real HTTP promoter. The unsafe auth-bypass patch produced `CRITICAL_TEST_FAILURE`; the dashboard displayed the finding and kept approval disabled. The corrected timeout patch passed the fixed authentication test; the dashboard reviewer entered a token, clicked **Approve exact patch** and **Request promotion**, saw `Promotion PROMOTED`, and the temporary Git repository contained the correction. This browser check used API-submitted previews rather than Kiro traffic in the same run; the Kiro-to-promotion path was verified separately above. The API and promoter used local memory/artifacts for this pass; earlier S3 evidence remains a separate verification.

**Next:** Ali and Kavin review and approve `integration/main` for merge. Only after both approve, merge it once into `main`. A single browser-driven dashboard launch through real Kiro, followed by review and promotion in that same run, remains unrecorded. AgentCore remains unimplemented.

## Ali: which branch to use now

The instructions in this section supersede older branch directions below, which are retained as history. There is no need to merge `codex/ali-review-safeguards` again or pull Person 2's branch into Ali's branch: both are already represented in `integration/main`.

| Branch | What Ali should do |
| --- | --- |
| `Ali-Person-1` | The mocked DynamoDB concurrency and SSE timing fixes are committed here at `074210c` and are already merged into `integration/main`. Continue future control-plane work here; update `docs/handoffs/person1-control-plane.md` with new results. |
| `kavinperson2branch` | Read the latest Person 2 handoff and execution-plane code if needed. Do not commit Ali-owned fixes here. Person 2 owns this branch; its current code is already in `integration/main`. |
| `codex/ali-review-safeguards` | No work needed. Its review-auth and Stop safeguards were already merged into `Ali-Person-1`; this branch is only historical. |
| `integration/main` | The current combined branch is tested as described above. Review it and record Ali's approval; do not make new Ali-owned fixes directly here. |
| `main` | Leave unchanged until Ali and Kavin both approve the combined branch. Then merge `integration/main` into `main` once. |

Current integration worktree on Kavin's machine: `/Users/kavins/Projects/IntentLock-integration`. Ali can use any clean checkout of `integration/main`; the checkout variables must point to that checkout. The prior failures and reproduction commands are preserved below as history.

## 2026-11-07 earlier integration merge gate — resolved by Ali (historical)

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
