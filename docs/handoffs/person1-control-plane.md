# Handoff — Person 1 (Control Plane)

## 1. Project and Workstream
- Product: IntentLock with TwinGuard (A10 Agent Harness hackathon)
- Owner: Person 1 (Ali). Workstream A: dashboard, API, Bedrock proposal, contract validation/approval/versioning, deterministic policy, run state/stop, events, DynamoDB/CloudWatch/infra, frontend integration.
- PRD: `intentlock-prd.md` (sections 9–12, 15, 17).

## 2. Last Updated
- UTC: 2026-11-07
- Branch: `Ali-Person-1` (the PRD calls it `person1-control-plane`; actual name is `Ali-Person-1`)
- Commit: `9a15fb9` (milestone 5: TwinGuard-facing API + Person 2 reconciliation). Earlier: `9ac6ea1` launch/trusted tests, `8064936` DynamoDB/Lambda, `57d3c5c` dashboard, `a66614d` foundation
- Schema version: `0.1.0` (PROPOSED — not yet agreed with Person 2)

## 3. Current Status
| Area | State |
|---|---|
| Shared schemas + fixtures (0.1.0) | Tested locally. Person 2 has ACCEPTED the six deviations in §8 (per their handoff) |
| Policy engine (PRD §12) | Tested locally |
| Backend API | Tested locally on memory store AND mocked DynamoDB; live `uvicorn` runs verified on both |
| Store interface (atomic ops) | Tested: identical contract suite on memory + mocked DynamoDB (24 tests x 2) |
| DynamoDB store (`dynamo_store.py`) | Implemented; **tested against moto (MOCKED AWS) only — not yet run on a real table** |
| Lambda handler (`lambda_handler.py`) | Implemented; tested with direct + Function-URL events against mocked DynamoDB; **never deployed or invoked on AWS** |
| Infra template (`infrastructure/control-plane/template.yaml`) | Written; `cfn-lint` clean; **not deployed** (account/region/target unconfirmed) |
| Bedrock contract proposal | Implemented but untested against real AWS (fake-invoker tests only; `boto3` now installed locally, no model configured) |
| CloudWatch | Lambda logs go to a CloudWatch log group once deployed; custom metrics NOT implemented |
| React dashboard (5 screens) | Tested locally (manual browser run; unit tests for pure logic) |
| TwinGuard-facing API + review/promotion UI | Tested locally (API: 40 tests on memory + mocked DynamoDB; UI: manual browser run against the real endpoints). **Impact reports, patches and the promoter are SIMULATED** (fixtures / fake promoter) because Person 2 has not built TwinGuard or the patch promoter |
| Agent launch from dashboard (control-plane side) | Tested locally: `POST /api/runs/{id}/launch` starts a CONFIGURED runner subprocess; verified in the browser against Person 2's real `launch_kiro.py` supervisor with a SYNTHETIC kiro stand-in. **Never run with real Kiro (not installed/logged in here)** |
| Trusted-test protection (authorization layer) | Tested locally (17 tests + shared vectors). TwinGuard side NOT implemented (Person 2) |
| Execution-plane integration | **Reconciled with `origin/kavinperson2branch@3847418`** (only code change: `--v3` in `launch_kiro.py`; schema copies byte-identical to `packages/contracts`). Person 2's suite passes against this branch. Person 2 independently reported REAL authenticated Kiro 3.1 traffic against commit `1bfd0ff` (verified by them, not by me). **Dashboard→real Kiro has NOT been run** (Kiro not installed/authenticated on this machine); procedure for Kavin's machine: `tests/integration/README.md` |

## 4. Completed Features
- **Canonical schemas** — `packages/contracts/schemas/*.schema.json`, fixtures in `packages/contracts/fixtures/`, models in `packages/policy-engine/intentlock_policy/models.py`. Tested locally.
- **Deterministic policy** — `packages/policy-engine/intentlock_policy/engine.py`, `paths.py`. Pure function, fail-closed, 12-step order from PRD §12. Tested locally.
- **Redaction** — `.../redact.py`: events store only redacted args (patch → sha256 + byte count). Tested locally.
- **API** — `apps/api/intentlock_api/{app,store,bedrock}.py`. Tested locally via ASGI TestClient and one live `uvicorn` + `curl` smoke run (allow → deny `.env` → `RUN_NOT_ACTIVE`).

### Dashboard (milestone 2) — `apps/web/`
Vite + React 18 + TypeScript, hash routing, no UI library. Start: `cd apps/web && npm install && npm run dev` (http://localhost:5173; `/api` and `/health` proxied to the API at `VITE_API_PROXY`, default `http://127.0.0.1:8787`).
| Route | Screen | Backend data used |
|---|---|---|
| `#/` | Create Mission | `POST /api/contracts/propose`; fixture fallback (clearly labelled "Bedrock bypassed") |
| `#/review` | Review Contract (all fields editable) | `POST /api/contracts` (`approved:true`) then `POST /api/runs` |
| `#/runs/{id}` | Active Run | `GET /api/runs/{id}` (poll 2s), SSE `/events/stream`, `GET .../events`, `POST .../stop`, `GET /api/contracts/...` |
| `#/runs/{id}/impact` | TwinGuard Impact Review | events (`impact_report` + `details.impact_report_id`), `GET /api/impact/{id}`, `GET /api/impact/{id}/patch`, `POST .../approve|reject|promote` |
| `#/runs/{id}/audit` | Final Audit | run, contract, events, impact report (if available); "Export JSON" of exactly that data |
Env vars (web): `VITE_API_URL` (empty in dev), `VITE_API_PROXY`, `VITE_DEMO_BASE_COMMIT` (prefill). See `apps/web/.env.example`.
Behaviour: header shows real `/health` (API connected / store label / schema version). SSE uses `EventSource` (auto-reconnect, resumes via `Last-Event-ID`); a 2s poll reconciles and keeps state correct if SSE is blocked; states shown: connecting / live / reconnecting / closed(+retry) / API unreachable / run not found. Stop button posts, then renders the run object RETURNED by the backend; it is disabled once the run is terminal. Approve is disabled for `decision=="block"` and until the patch text loaded; Promote only after a successful approve response. Nothing is fabricated: absent data renders as "unavailable / not reported".

### Milestone 3 — persistence and Lambda
- **Store interface** (`apps/api/intentlock_api/store.py`): `put_contract` (raises `ContractExists`; versions immutable, only `status` changes), `transition_run(run_id, from_statuses, **fields)` (atomic compare-and-set; returns `None` for the loser), `reserve_tool_call` (atomic counter, returns prior count), `append_event` (gap-free `seq` per run; `event_id = evt_{run_id}_{seq:08d}`), `update_event`, `list_events(after_seq)`, `put_review/list_reviews`. `LocalMemoryStore` is the explicit fallback.
- **DynamoDB store** (`dynamo_store.py`, single table pk/sk): contracts `CONTRACT#id/V#00000001` (Put with `attribute_not_exists`); run `RUN#id/META` with native attrs + `event_seq` + `tool_calls`; events `RUN#id/EVT#000000000001`; reviews `REVIEW#impact/R#ts#id`. Run transitions use `ConditionExpression` on `status`; tool-call counting is `ADD tool_calls 1`; event append is ONE `TransactWriteItems` (counter bump + event Put) so readers never see a gap; all reads are consistent; thread-local boto3 resources.
- **Config**: `INTENTLOCK_STORE=memory|dynamodb` (default memory), `INTENTLOCK_TABLE`, `AWS_REGION`, `INTENTLOCK_DDB_ENDPOINT` (emulator only). `/health.store` reports the active backend. Store/infra failure ⇒ `/internal/authorize` returns a DENY `POLICY_ENGINE_ERROR` decision (HTTP 200), other endpoints 503 `STORE_UNAVAILABLE`.
- **Shared service** (`service.py`): used by BOTH `/internal/authorize` and the Lambda: expire overdue run (atomic, one `run_stopped` event) → load state → reserve call slot → write pending event → pure policy → if the run was stopped meanwhile turn ALLOW into `RUN_NOT_ACTIVE` → record decision → atomic stop transition when `stop_run`.
- **Lambda** (`lambda_handler.py`, handler `intentlock_api.lambda_handler.handler`): direct-invoke event = ToolRequest → AuthorizationDecision; Function-URL/API-GW proxy event (`body`, optionally base64) → `{statusCode:200, body}`. Any error/misconfig ⇒ DENY. The deployable zip (`build_lambda.sh`) excludes `app.py` and `bedrock.py`; a test asserts the handler never imports Bedrock. IAM-authenticated callers only; AgentCore interceptor payload mapping NOT implemented/verified.
- **New endpoint** (requested by Person 2): `POST /internal/runs/{run_id}/state` — see §7.

### Milestone 4 — real agent launch + trusted tests (2026-11-07)
- **Launcher** (`apps/api/intentlock_api/launcher.py`, LOCAL-DEV only, not used by Lambda): the control plane spawns the execution plane's runner as a subprocess. Command is FIXED by server config (`INTENTLOCK_AGENT_LAUNCH_ARGV`, JSON array, no shell, placeholders `{run_id} {contract_id} {contract_version} {mission}`; use `--prompt={mission}`); requests supply only a run id. Child env is an allowlist (PATH, HOME, USER, LANG, LC_ALL, TMPDIR, KIRO_CLI_BINARY + `INTENTLOCK_API_URL`/`INTENTLOCK_INTERNAL_TOKEN`); `AWS_*` is never forwarded even if listed. Runner stdout/stderr go to a private log dir (never served). One launch per run. A watcher thread: when the run is terminal and the runner is still alive after `INTENTLOCK_AGENT_STOP_GRACE_SECONDS` (default 3) it escalates against the RUNNER's process group: SIGINT → +2s SIGTERM → +4s SIGKILL; when the runner exits and the run is still active it sets `completed` (exit 0) or `failed` (non-zero; reason `agent launcher exited with code N`) via the same atomic transition; a runner that already reported state is not overwritten.
- **API:** `GET /api/launcher`, `POST /api/runs/{id}/launch` (201 / 409 `RUN_NOT_ACTIVE|CONTRACT_NOT_ACTIVE|ALREADY_LAUNCHED` / 503 `AGENT_LAUNCHER_NOT_CONFIGURED` / 502 `AGENT_LAUNCH_FAILED` which also marks the run `failed`), `GET /api/runs/{id}/launch` → `{launched, launcher_pid, alive, exit_code, started_at, escalation}`. `POST .../stop` now returns `process_termination` ∈ `launcher_watcher_armed | launcher_already_exited | delegated_to_execution_plane`; it never claims the process is dead.
- **Dashboard:** "Approve and launch" creates the contract and run, then asks the backend to launch (failure is not hidden); the Active Run screen shows the real launcher state (running/exited+code/not configured/not launched), a manual "Launch agent" button when configured, the agent PID Person 2's supervisor registered, and backstop signals if the watcher had to escalate.
- **Trusted tests** (`engine.py: _check_trusted_tests`, deterministic, `propose_patch` only; reads stay allowed): a patch is denied `PATH_NOT_ALLOWED` with `evidence.rule="TRUSTED_TEST_READ_ONLY"` (no stop-run) if it creates/modifies/deletes/renames any path matching `tests/**`, `test/**`, `__tests__/**`, `**/tests/**`, `**/test/**`, `**/__tests__/**`, `*.test.*`, `*.spec.*`, `jest.config.*`, `vitest.config.*`, `.mocharc*`, `jest.setup.*`, `setupTests.*` (case-insensitive), any repo-relative file named in an approved test's argv, or `package.json` entries that change `scripts`, lifecycle scripts (`install/preinstall/postinstall/prepare/…`), anything starting `test`, or the script name(s) referenced by approved argv (`npm|pnpm|yarn run X`, plus `preX/postX`); plus any create/delete/rename of `package.json`. It overrides the contract's `allowed_paths` (the PRD example allows `tests/auth/**`; that now only permits READING). Protected-path precedence is unchanged. No schema change: uses existing reason code + free-form `evidence`. Shared test vectors: `packages/contracts/fixtures/patch-vectors/trusted-tests.json`.
- **Environment:** project venv `.venv` (gitignored) with `apps/api/requirements.txt` / `requirements-dev.txt`; `apps/api/.env.example` lists all variables.

### Milestone 5 — TwinGuard-facing API (2026-11-07)
New modules `apps/api/intentlock_api/{impact,artifacts,promoter}.py`; store methods `put_impact/get_impact`, `put_final_review/get_final_review` (atomic first-decision-wins), `put_promotion/get_promotion/transition_promotion` on both stores (DynamoDB keys `IMPACT#id/REPORT`, `REVIEW#id/FINAL`, `PROMO#id/STATE`). **The backend, not the frontend, enforces everything below.** Endpoint schemas are PROPOSED for Person 2 (see §9) and not frozen until they agree; no shared schema was changed.
- `POST /internal/impact` (token): execution plane submits an `ImpactReport` (shared schema 0.1.0, strict). Run must exist; `contract_id/version` must match the run (`409 IMPACT_MISMATCH`); `base_commit` must equal the run's (`409 STALE_BASE_COMMIT`); immutable (re-submit identical → 200 idempotent, different → `409 IMPACT_EXISTS`); records an `impact_report` event. **Never triggers promotion.**
- `GET /api/impact/{id}` → the report. `GET /api/impact/{id}/patch` → the exact artifact bytes (`text/plain`, `X-Patch-SHA256`), served ONLY if SHA-256(bytes) == `patch_sha256` (`409 PATCH_HASH_MISMATCH` otherwise, `404 PATCH_UNAVAILABLE` if missing/unreadable). Artifacts are read via `INTENTLOCK_ARTIFACT_DIR` (LOCAL DEV stand-in for the S3 layout `runs/{run_id}/patches/{sha256}.patch`; path traversal/symlink escape rejected; an S3 reader is NOT implemented).
- `GET /api/impact/{id}/status` → `{decision, review, promotion, can_approve, approve_blockers[], can_promote, promote_blockers[], reviewer_policy, promoter_configured, artifact_reader}` — the UI renders this; it never decides.
- `POST /api/impact/{id}/approve|reject` body `{reviewer, patch_sha256, base_commit, comment?}`. Approve is refused (409, code as `detail`) when: `IMPACT_BLOCKED` (a blocked patch is NEVER approvable), `ALREADY_REVIEWED`, `RUN_NOT_ELIGIBLE` (run `blocked`/`failed`), `STALE_BASE_COMMIT`, `CONTRACT_NOT_ACTIVE` (superseded/revoked), `PATCH_HASH_MISMATCH`/`PATCH_UNAVAILABLE` (the artifact is re-read and re-hashed at approval time), or body digest/base commit differ from the report. `403 REVIEWER_NOT_AUTHORIZED` when `INTENTLOCK_REVIEWERS` (comma list) is set and the reviewer is not on it; with no allowlist any non-empty identity is accepted (LOCAL DEV, self-asserted — the public API has no real authentication). Exactly one decision per report (atomic); same reviewer repeating is idempotent; a different decision/reviewer → `409 ALREADY_REVIEWED`. The record is a shared-schema `ReviewApproval` (reviewer, `contract_version`, `base_commit`, `patch_sha256`, timestamp) + a `review_decision` event. Reject is allowed even for blocked reports.
- `POST /api/impact/{id}/promote` body `{requested_by, patch_sha256}`: requires an `approve` review; digest must equal the approved one; re-checks contract/run/base-commit/artifact hash; at most one request per report (atomic, `409 ALREADY_REQUESTED`); `503 PROMOTER_NOT_CONFIGURED` (no record created) if `INTENTLOCK_PROMOTER_URL`/`INTENTLOCK_PROMOTER_TOKEN` are unset. It then calls Person 2's promoter (`POST {url}/promote`, header `X-IntentLock-Promoter-Token`, no redirects, https unless localhost) with the approved identity/digest/base commit and records the outcome: `promoted`, `refused` (+reason code), or `failed` (`PROMOTER_UNAVAILABLE`). If the promoter claims success for a different digest/base commit the record is `failed` `PATCH_HASH_MISMATCH` (never reported as success). Records a `promotion` event. **This code never applies a patch; only the promoter does, and it must re-verify.**
- Event sanitizer now preserves values under digest keys (`patch_sha256`, `applied_patch_sha256`, `base_commit`, `sha256`, `commit`, `resulting_commit`) only when exactly 40/64 lowercase hex; all other long tokens are still redacted.
- Dashboard: the Impact screen's Approve/Reject/Request-promotion buttons are driven by `/status` (`can_approve`, `can_promote`, blocker explanations); it shows the recorded review and promotion; the Final Audit shows review + promotion records and exports them.

## 5. Current Implementation Work
Dashboard is functional against the local API. Next per instruction: integrate Person 2's MCP traffic, then DynamoDB, Lambda, CloudWatch, real Bedrock verification. Nothing half-finished in the tree.

## 6. Files Created or Modified
`AGENTS.md` (shared root file — new; Person 2 must review/integrate), `.gitignore`, `pytest.ini`, `packages/contracts/**` (shared dir, proposed), `packages/policy-engine/**`, `apps/api/**`, `tests/contract/**`, `tests/policy/**`, this handoff. Milestone 2 added `apps/web/**`, small `apps/api` changes (lazy run expiry, `Last-Event-ID`), extra tests. `.gitignore` gained `dist/`. `packages/event-model/` is an empty placeholder (Event schema lives in `packages/contracts`).

Milestone 3: `apps/api/intentlock_api/{store,dynamo_store,service,lambda_handler,app}.py` (store refactor, new modules), `apps/api/tests/{conftest,test_store_contract,test_lambda_handler,test_api}.py`, `infrastructure/control-plane/{template.yaml,build_lambda.sh,README.md}`, `.gitignore` (`infrastructure/control-plane/build/`), this handoff.

Milestone 4: `apps/api/intentlock_api/{launcher,app}.py`, `apps/api/tests/test_launcher.py`, `apps/api/requirements*.txt`, `apps/api/.env.example`, `packages/policy-engine/intentlock_policy/engine.py`, `tests/policy/test_trusted_tests.py`, `packages/contracts/fixtures/patch-vectors/trusted-tests.json` (shared dir, proposed), `tests/integration/test_agent_launch_e2e.py`, `apps/web/src/{lib/api.ts,lib/types.ts,screens/ActiveRun.tsx,screens/ReviewContract.tsx}`, `AGENTS.md` (venv guidance), `.gitignore`.

Milestone 5: `apps/api/intentlock_api/{impact,artifacts,promoter,store,dynamo_store,app}.py`, `packages/policy-engine/intentlock_policy/redact.py`, `apps/api/tests/{test_impact_api,test_store_contract}.py`, `tests/policy/test_policy.py`, `apps/web/src/{lib/api.ts,lib/types.ts,screens/ImpactReview.tsx,screens/FinalAudit.tsx}`, `tests/integration/README.md`, `infrastructure/control-plane/README.md`.

## 7. API Endpoints
Base URL (local): `http://127.0.0.1:8787` — start: `cd apps/api && PYTHONPATH=../../packages/policy-engine:. INTENTLOCK_INTERNAL_TOKEN=<token> python3 -m uvicorn intentlock_api.app:create_app --factory --port 8787`

Public (no auth in hackathon build — demo only):
| Method | Path | Notes |
|---|---|---|
| GET | `/health` | `{status, schema_version, store}` |
| POST | `/api/contracts/propose` | `{task, repository_id?, base_commit}` → `{draft, attempts, status:"proposed", requires_human_approval:true}`. Needs Bedrock (`INTENTLOCK_BEDROCK_MODEL_ID`, AWS creds); else 503. Never creates a contract. |
| POST | `/api/contracts` | `{draft, approved:true, approved_by, contract_id?}` → MissionContract (201). `approved` must be `true` (else 400 `APPROVAL_REQUIRED`). Passing `contract_id` creates version+1 and marks the previous `superseded`. |
| GET | `/api/contracts/{id}/versions/{v}` | MissionContract |
| POST | `/api/runs` | `{contract_id, contract_version}` → Run (status `active`, 201). 409 if contract not active. |
| GET | `/api/runs/{run_id}` | Run |
| POST | `/api/runs/{run_id}/stop` | `{reason?}` → `{run, state_committed:true, process_termination:"delegated_to_execution_plane"}`. State committed before response; idempotent. |
| GET | `/api/runs/{run_id}/events?after_seq=N` | `{events:[Event]}` ordered by `seq` |
| GET | `/api/runs/{run_id}/events/stream?after_seq=N&timeout_seconds=300` | SSE (`id`=seq, `event`=event_type, `data`=Event JSON) |

Internal (header `X-IntentLock-Internal-Token: $INTENTLOCK_INTERNAL_TOKEN`; if the env var is unset the internal API returns 503 — fails closed):
| Method | Path | Notes |
|---|---|---|
| POST | `/internal/authorize` | Body = ToolRequest. Always HTTP 200 with an AuthorizationDecision (malformed bodies → `deny INVALID_ARGUMENTS`). 401/503 only for auth problems. |
| POST | `/internal/runs/{run_id}/agent` | `{agent_process_id?, session_id?, sandbox_id?}` → Run |
| POST | `/internal/events` | `{run_id, event_type ∈ agent_event|impact_report|review_decision|promotion|run_completed, message?, request_id?, details?}` → Event (201). `details` is sanitised (sensitive keys/secret-like strings redacted). |

API changes in milestone 2: (a) overdue active runs are marked `stopped` (`stop_reason: "run_timeout"`, one `run_stopped` event) lazily on any access — authorize, GET run/events, SSE loop — BEFORE the policy runs, so state and the `RUN_NOT_ACTIVE` denial agree (no background reaper); (b) SSE honours the `Last-Event-ID` header.

NOT implemented yet (PRD §15): `GET /api/impact/{id}`, `POST /api/impact/{id}/approve|reject|promote`, `/internal/sandboxes*` (Person 2 owns the execution logic; control plane will proxy/record).

Example authorize call:
```
curl -XPOST $BASE/internal/authorize -H "X-IntentLock-Internal-Token: $T" -H 'content-type: application/json' -d '{"request_id":"req_02","run_id":"run_…","contract_id":"contract_…","contract_version":1,"tool":"read_file","arguments":{"path":".env"},"agent_id":"coding-agent-01","timestamp":"2026-11-07T10:01:00Z"}'
→ {"request_id":"req_02","decision":"deny","reason_code":"PROTECTED_PATH",…,"stop_run":true,"latency_ms":0.035}
```

Added in milestone 3 (internal, token required): `POST /internal/runs/{run_id}/state` body `{"status":"completed"|"failed","reason"?}` → `{run, transitioned}`. Atomic active/created → terminal; first writer wins (a stopped/blocked run is NOT overwritten and returns `transitioned:false`); records one `run_completed` event. This is the authoritative completion path for the execution plane (events alone never change `Run.status`).
Changed behaviour: `/internal/authorize` no longer relies on a process-local lock; correctness comes from the store's atomic operations. Event IDs are now deterministic (`evt_{run_id}_{seq:08d}`); schema unchanged.

Milestone 5 endpoints (impact/review/promotion) are documented in §4 (Milestone 5). Internal: `POST /internal/impact`. Unchanged and compatible with Person 2: `/internal/authorize`, `/internal/runs/{id}/agent`, `/internal/runs/{id}/state`, `/internal/events`.

## 8. Shared Schemas (0.1.0, proposed)
Types: MissionContract, MissionContractDraft, ToolRequest, AuthorizationDecision, Run, Event, ImpactReport, ReviewApproval. JSON Schemas and fixtures in `packages/contracts/`. All strict (no extra fields). Deviations/additions vs PRD that Person 2 must accept or reject:
1. `network_access` is constant `false` (scope freeze).
2. `Run.status` ∈ created|active|stopped|blocked|completed|failed. `PROTECTED_PATH` denial ⇒ run becomes `blocked`.
3. `Event` is one record per tool request: created `pending` before authorization, updated to `decided`; plus `seq` (per-run, monotonic) for polling/SSE; other types: run_started, run_stopped, agent_event, impact_report, review_decision, promotion, run_completed.
4. `ImpactReport.decision` ∈ block|review_required|pass ("pass" = may go to human review, never auto-apply); test status ∈ passed|failed|timeout|error; finding severity ∈ critical|high|medium|low|info. `ReviewApproval` fields invented (PRD gives none) — see fixture.
5. Run-timeout denial reuses `RUN_NOT_ACTIVE` with `evidence.cause="run_timeout"` (no dedicated code in PRD list). Proposed: add `RUN_TIMEOUT` in 0.2.0 if wanted.
6. `request_permission` and `finish_task` are exempt ONLY from the `allowed_tools` list (no repo effect). They are NOT unconditionally authorized: they still require a matching, active run and contract at the exact version, valid arguments, and count toward / are denied by `max_tool_calls` and the run timeout (tests: `test_always_allowed_tools_obey_run_state_limits_and_args`, `test_request_permission_and_finish_task_are_conditional`). If you want them fully subject to the allowlist, say so — one-line change.

## 9. Integration Information for Person 2
- Authorize every tool call BEFORE invoking the tool: POST `/internal/authorize`; invoke only if `decision=="allow"`. If `stop_run` is true (or later `RUN_NOT_ACTIVE`), kill the agent process.
- Run lifecycle: control plane creates the run (`POST /api/runs`, from the UI); you receive/know `run_id`, `contract_id`, `contract_version`. Report process/sandbox ids via `/internal/runs/{id}/agent`. Poll `GET /api/runs/{id}` or react to `stop_run`/`RUN_NOT_ACTIVE` to terminate.
- Stop: UI → `POST /api/runs/{id}/stop` flips status to `stopped` first; all later authorize calls return `RUN_NOT_ACTIVE`. Actual process kill is YOURS (response says `delegated_to_execution_plane`).
- Env vars: control plane `INTENTLOCK_INTERNAL_TOKEN` (required for internal API), `INTENTLOCK_CORS_ORIGINS`, `INTENTLOCK_BEDROCK_MODEL_ID`, `AWS_REGION`. MCP server needs the API base URL + the same token (suggest `INTENTLOCK_API_URL`, `INTENTLOCK_INTERNAL_TOKEN`).
- **Requirements on your side (security-relevant):**
  - `search_code` globs are only checked up to their wildcard-free prefix: you MUST filter results through protected rules (baseline `.git`, `.env*` at any depth + contract `protected_paths`) and `allowed_paths`.
  - `propose_patch` is authorized on the paths named in diff headers (a/ b/ prefixes required, ambiguous/quoted headers denied). TwinGuard must still compute actual changed paths from the sandbox result and enforce protected paths/limits/symlinks/binary (PRD §13) — IntentLock does not check limits other than tool-call count and run timeout.
  - `list_files("src")` for a dir not matching an allowed glob is denied; you must also filter listings.
  - Never send file contents/patches in `/internal/events` details.
- Validate against `packages/contracts/schemas/*.json` (e.g. ajv).
- **Interfaces the dashboard expects from the execution plane (exact; not built — please confirm or counter-propose):**
  1. Report TwinGuard result: `POST /internal/events` with `{"run_id","event_type":"impact_report","message":"…","details":{"impact_report_id":"impact_01"}}`. The UI finds the report via the latest such event.
  2. `GET /api/impact/{impact_report_id}` → ImpactReport JSON (schema `impact-report`, 0.1.0).
  3. `GET /api/impact/{impact_report_id}/patch` → `text/plain` unified diff of the EXACT artifact whose SHA-256 is `patch_sha256` (UI will not enable Approve without it).
  4. `POST /api/impact/{id}/approve|reject` body `{"reviewer","patch_sha256","base_commit"}` → any JSON object (shown verbatim). Must refuse `decision=="block"`, and verify digest/base commit.
  5. `POST /api/impact/{id}/promote` body `{"patch_sha256"}` → JSON result (shown verbatim); emit an `event_type:"promotion"` via `/internal/events` for the audit screen.
  6. Also emit `review_decision`, `agent_event`, `run_completed` events as appropriate; `details` must never contain contents/patches.
  Open question: who serves 2–5 (the control-plane API proxying to your service, or your service directly behind the same origin). The UI calls them on the control-plane origin.
- Note: `intentlock-prd.md` is NOT committed to the repo (shared root file; whoever adds it should tell the other). Blockers: none known; waiting on Person 2's branch/handoff (not present on `origin` as of this update).

**Person 2 handoff read (branch `origin/codex/person2-execution-plane`, commit `8a93b37`, read-only via `git show`).** Their stated contract matches this API: HTTP + `X-IntentLock-Internal-Token`, `INTENTLOCK_API_URL`, `/internal/authorize`, `/internal/runs/{id}/agent`, polling `GET /api/runs/{id}`. They accepted the §8 deviations and chose HTTP over direct Lambda. Their requests to me and status:
1. Completion/failure transition endpoint → DONE (`/internal/runs/{id}/state`, §7).
2. Authorization + event writes atomic with DynamoDB → DONE (§4; verified on mocked DynamoDB).
3. Wire the dashboard launch to an execution-plane runner → NOT done: needs an agreed runner interface (who starts the agent for a `run_id`; proposal: runner service or CLI that is given `run_id/contract_id/contract_version` and registers PID via `/internal/runs/{id}/agent`).
4. Trusted test catalogue outside agent-writable scope → open (contract semantics: `approved_tests` argv is fixed, but the repo's test files are agent-editable; needs an agreed trusted-path rule, e.g. protect `tests/auth/**`/`package.json` in the demo contract).
5. They also keep schema snapshot copies in `apps/mcp-server/schemas`; canonical remains `packages/contracts`.
Lambda is an ALTERNATIVE transport (e.g. for AgentCore later); they do not need it now.

**Configuration for MCP integration (HTTP):** API base URL (`http://127.0.0.1:8787` local), `INTENTLOCK_INTERNAL_TOKEN` (same value on both sides), request = ToolRequest, response = AuthorizationDecision (always HTTP 200 for `/internal/authorize`; 401 bad token, 503 token unset). Honor `stop_run:true`/`RUN_NOT_ACTIVE` by terminating the agent. Report completion with `/internal/runs/{id}/state`. Run API on DynamoDB with `INTENTLOCK_STORE=dynamodb` etc. (see `infrastructure/control-plane/README.md`).

### Milestone 4 — exact requirements for Person 2 (read this)
Read from `origin/codex/person2-execution-plane@8a93b37` (unchanged since last check). Their runner interface as I integrated it: `python3 -B apps/mcp-server/launch_kiro.py --repository <repo> --run-id R --contract-id C --contract-version V [--prompt P]` with env `INTENTLOCK_API_URL`, `INTENTLOCK_INTERNAL_TOKEN`, optional `KIRO_CLI_BINARY`; it is also the supervisor. Control plane launches it via `INTENTLOCK_AGENT_LAUNCH_ARGV` (example in `apps/api/.env.example`). **No change to their interface was needed for launch.** What I need from them:
1. **Pass the mission as the prompt.** Their default `--prompt` is a hard-coded synthetic string. The control plane passes `--prompt={mission}`; confirm it is used as the Kiro prompt and still treated as untrusted-by-prompt-injection data.
2. **Fix an intermittent supervisor defect** in `lifecycle.terminate_group`: the final `os.killpg(pid, SIGKILL)` can raise `PermissionError` on macOS (seen once during a dashboard stop; the agent and MCP server were gone, but the supervisor exited 1 and skipped its final event). Catch `(ProcessLookupError, PermissionError)` around both `killpg` calls. Test: `tests/integration/...::test_supervisor_exits_cleanly_with_lifecycle_event_after_stop` (xfail-tolerant).
3. **Signals:** the runner only cleans up the agent group on SIGINT (KeyboardInterrupt). Add a SIGTERM handler that runs the same cleanup. Until then the control plane's backstop sends SIGINT first, then SIGTERM, then SIGKILL (SIGTERM/SIGKILL would orphan the agent group).
4. **Report completion** with `POST /internal/runs/{id}/state {"status":"completed"|"failed","reason"?}` when the agent task ends; otherwise the launcher watcher infers `completed` (exit 0) / `failed` (non-zero) for runs it launched.
5. **TwinGuard must enforce the trusted-test rule too** (authorization alone is not sufficient; the dashboard/Lambda path could change, and patches are re-applied in the sandbox):
   a. Run approved tests from a TRUSTED BASELINE: materialize test files, test config, and `package.json` scripts from `base_commit` (e.g. `git archive base_commit`), apply the patch, then restore/overlay the baseline copies of every trusted path before executing the approved argv. The command comes only from the contract's argv; resolve `npm run X` against the baseline `package.json`.
   b. After applying the patch, compute the changed-path set from the sandbox result (not from the request) and emit a critical finding `TRUSTED_TEST_MODIFIED` ⇒ `decision: "block"` if any trusted path (same patterns as §4 Milestone 4, including `package.json` script/lifecycle keys and files named in approved argv) is created, modified, deleted, renamed, symlinked or mode-changed. Compare content hashes against the baseline as a second check.
   c. Reuse `packages/contracts/fixtures/patch-vectors/trusted-tests.json` (`twinguard_expect` per vector). Dependency-only `package.json` changes remain `review_required` (PRD §13.2). Use finding code `TRUSTED_TEST_MODIFIED` (new finding code only; `findings[].code` is free-form in schema 0.1.0 — no schema change). Please confirm the code name.
6. Still open from before: who serves `/api/impact/*`; list/search tools need per-result protected filtering; AgentCore mapping.

**Do not edit** `packages/contracts/schemas/*` or the policy engine; propose changes in your handoff instead.

### Milestone 5 — reconciliation with `origin/kavinperson2branch@3847418` and TwinGuard contract (read this)
**Compatibility verified (read-only, no files of theirs modified):** their three schema copies (`tool-request`, `authorization-decision`, `run`) are byte-identical (parsed-JSON equal) to `packages/contracts/schemas` 0.1.0; the MCP→`/internal/authorize` and `/internal/runs/{id}/agent` HTTP usage matches this API; their only change since `8a93b37` is `--v3` in `launch_kiro.py` (Kiro V3 profile loading); their suite (15 tests) passes against this branch at `9a15fb9`; my launch e2e passes against their branch. Their handoff predates my milestones 3–5 and still lists "no completion endpoint": **it exists.**
**Completion endpoint for the runner (`POST /internal/runs/{run_id}/state`, header `X-IntentLock-Internal-Token`):** body `{"status":"completed"|"failed","reason"?:str<=200}` → `{"run":Run,"transitioned":bool}`. Atomic active→terminal, first writer wins (a run already `stopped`/`blocked`/`completed` is NOT overwritten; `transitioned:false`), records one `run_completed` event, afterwards `/internal/authorize` returns `RUN_NOT_ACTIVE`. Suggested use in their `control_plane.Client`: call after the agent process exits (`completed` on exit 0, else `failed`). If they do not, the control plane's launcher watcher infers it for runs it launched.
**TwinGuard contract (PROPOSED, please confirm or counter-propose; I will not freeze it without you):**
1. Submit each result: `POST /internal/impact` with an `ImpactReport` (shared schema `impact-report` 0.1.0; `decision` ∈ `block|review_required|pass`; `contract_id/contract_version/base_commit` must equal the run's; `patch_sha256` = SHA-256 of the patch bytes you stored).
2. Store the exact patch bytes at `patch_s3_key` (PRD layout `runs/{run_id}/patches/{sha256}.patch`). Local dev: write under the directory the control plane is given as `INTENTLOCK_ARTIFACT_DIR`. The control plane re-hashes before serving or approving.
3. Promoter: implement `POST /promote` (header `X-IntentLock-Promoter-Token`, localhost http or https) accepting `{impact_report_id, run_id, contract_id, contract_version, base_commit, patch_sha256, patch_s3_key, review:{review_id, reviewer, reviewed_at, decision:"approve"}}` and returning `{status:"promoted", base_commit, applied_patch_sha256}` or `{status:"refused", reason_code, message?}`. The promoter MUST independently verify: repository HEAD == `base_commit` (else refuse `STALE_BASE_COMMIT`), SHA-256 of the artifact == `patch_sha256` (else `PATCH_HASH_MISMATCH`), `git apply --check` before applying, and apply ONLY that artifact; it must never re-run the agent action. The control plane's approval is a precondition, not a substitute for these checks. Reply bodies are strict (extra fields rejected); `applied_patch_sha256`/`base_commit` must echo the approved values.
4. Trusted tests/`TRUSTED_TEST_MODIFIED`: unchanged from milestone 4 (§9 above). Authorization layer (done): deterministic `PATH_NOT_ALLOWED`/`TRUSTED_TEST_READ_ONLY` for test paths, test configs, `package.json` test/lifecycle script keys, approved-test script names and files named in approved argv (`packages/contracts/fixtures/patch-vectors/trusted-tests.json`). TwinGuard side (yours, NOT done): run approved tests from the base-commit baseline; block with finding code `TRUSTED_TEST_MODIFIED` if any trusted path/script changes in the sandbox result. Please confirm the finding code and the pattern list.

**Person 2 issue tracker (none of their files touched):**
| # | Item | Status |
|---|---|---|
| 1 | Intermittent macOS `os.killpg(SIGKILL)` `PermissionError` in `lifecycle.terminate_group` | OPEN (reproduced once in a manual dashboard stop; `test_supervisor_exits_cleanly_...` is xfail-tolerant) |
| 2 | Runner only cleans up on SIGINT; no SIGTERM handler | OPEN (my watcher escalates SIGINT→SIGTERM→SIGKILL) |
| 3 | `--prompt={mission}` from the control plane used as the Kiro prompt | UNCONFIRMED (their default `--prompt` is a synthetic string; my launcher always passes the mission) |
| 4 | Completion/failure via `/internal/runs/{id}/state` | endpoint READY on my side; their client does not call it yet |
| 5 | Kiro built-in file/shell tools cannot bypass the MCP gateway | UNVERIFIED (profile lists only `@intentlock/read_file` and denies builtin capabilities; no interactive tool-exposure inspection recorded). Do NOT claim all Kiro access is mediated. Inspection checklist: `tests/integration/README.md` |
| 6 | Only `read_file` exposed; list/search/propose_patch/run_approved_test pending | OPEN (list/search need per-result protected filtering) |
| 7 | TwinGuard sandbox, impact analyzer, patch promoter | NOT IMPLEMENTED (contract above is waiting on them) |

### Update 2026-11-07 (late): Person 2's latest state and the safeguards branch
- `origin/kavinperson2branch@7205a3d`: only the handoff changed since `3847418`; it now acknowledges `/internal/runs/{id}/state` and the dashboard launch, and reports REAL Kiro 3.1 MCP traffic on their machine (one ALLOW for `src/auth/session.ts`, then `PROTECTED_PATH` for `.env`). Kavin owns all Kiro work; Person 1 does not run Kiro.
- **`origin/codex/ali-review-safeguards@8026498`** (made by Person 2's side, based on `a92b199`, edits Person-1-owned files): reviewer-specific tokens (`INTENTLOCK_REVIEWER_TOKENS`, JSON name→token, header `X-IntentLock-Reviewer-Token`, constant-time compare) required for approve/reject/promote (401 `REVIEWER_NOT_AUTHENTICATED`, fail-closed `REVIEW_AUTH_NOT_CONFIGURED` when none are configured); runs in status `stopped` become ineligible for approval/promotion (previously only `blocked`/`failed`); dashboard asks for the token. **My review (read-only): the change is correct and closes the "self-asserted reviewer" gap I documented; its own tests pass (175 in impact+policy+contract, run in a separate worktree); a dry-run merge conflicts only in `infrastructure/control-plane/README.md` (docs).** I have NOT merged it (no authorization from the owner; Person 2's handoff asking for the merge is not authorization). Awaiting the owner's decision. Note it means a stopped run's patch can no longer be approved.

## 10. AWS Configuration
**One shared AWS account** (owned by Kavin): account `247495661854`, region `us-east-1`, IAM user `aparpia`, CLI profile `intentlock`, single environment. Stacks: Person 1 `intentlock-control-plane-dev`; Person 2 (proposed) `intentlock-execution-plane-dev`. Ownership/naming table: `infrastructure/control-plane/README.md` ("ONE shared AWS account"). **No credentials are stored in this repo or this file** (tracked files and full history scanned clean). The previously exposed access key must be deactivated by Kavin and never reused. The unrelated `default` profile is never used.
| Item | State (2026-11-07) |
|---|---|
| AWS CLI | Installed (`aws-cli/2.36.40`). |
| Profile `intentlock` | **Still does NOT exist** on this machine (re-checked after Kavin's clarification). Owner must run `aws configure --profile intentlock` privately (replacement key; region `us-east-1`; output `json`). |
| Identity / account / username match, service access, Bedrock model | **Not verified** until the profile exists. `infrastructure/control-plane/verify_aws_readonly.sh` is ready (read-only; checks identity, DynamoDB, Lambda, S3, CloudFormation incl. `validate-template`, CloudWatch/Logs, Bedrock listing, name conflicts, budgets, IAM simulation; never invokes a model). |
| Nothing deployed | No AWS resource has been created, changed or deleted. Earlier read-only checks used the unrelated `default` profile and do not describe the shared account. |
| Template | `cfn-lint` clean; resources now tagged `Project=IntentLock`, `Owner=person1-control-plane`. Not deployed. |
| Tests vs AWS | All tests use fake credentials / moto; no test can reach real AWS (see §11). |
Readiness, costs, $25 budget recommendation, least-privilege and public-endpoint reviews: `infrastructure/control-plane/README.md`.

## 11. Tests and Verification
- `python3 -m pytest -q` → **116 passed** (contract 29, policy 66, API 21). Executed 2026-11-07. Added: expiry-before-denial (incl. invalid-args request on expired run), plain-GET expiry, `Last-Event-ID` resume, `request_permission`/`finish_task` conditionality (run state, contract state/version, limits, arguments).
- Frontend: `cd apps/web && npx tsc --noEmit` clean; `npx vitest run` → 8 passed (event merge/dedupe, decision counts, latency percentiles, impact-id discovery, diff parser, error formatting); `npx vite build` OK. No component/E2E tests.
- Manual browser run (built-in browser, real API on :8787 + Vite on :5173): Bedrock failure shown honestly (`503 BEDROCK_UNAVAILABLE: ModuleNotFoundError` — boto3 not installed here); fixture draft → review → approve+launch → Active Run with live SSE; curl-simulated execution plane produced allow, allow, `.env` deny (run → BLOCKED, banner, stop reason), `RUN_NOT_ACTIVE`; agent id set via `/internal/runs/{id}/agent` appeared; stop button on a second run committed `stopped` and displayed backend state; Final Audit rendered real decisions; killing the API showed "API unreachable" and stream/poll error states; `impact_report` event ingestion made the Impact screen request `/api/impact/impact_01` and show "unavailable (404)".
- Impact review loaded state (block report with failed test + diff; pass report with approve → promote gating) was verified ONLY against a throwaway stub I ran outside the repo that serves the committed fixture. That proves the UI, not any real TwinGuard behaviour.
- Covered: all PRD §11-A acceptance items except "dashboard displays real events" (no dashboard yet) and CloudWatch logging checks; includes traversal/encoded/absolute paths, hidden patch headers, protected precedence, version mismatch, inactive/stopped, call/time limits, fail-closed on internal error, no secret/patch leakage in decisions/events/audit log, SSE.
- Manual: live `uvicorn` + `curl`: allow → `.env` deny (run → blocked) → `RUN_NOT_ACTIVE`. Latency ≈0.01–0.35 ms in-process (not an end-to-end/AWS figure).
- Missing: component/E2E tests for the dashboard; DynamoDB/Lambda/Bedrock integration tests, concurrency across processes (current counting uses a single-process lock), cross-workstream integration.

Milestone 3 results (2026-11-07):
- `python3 -m pytest -q` → **185 passed, 1 skipped** (contract 29, policy 66, API+store+Lambda 90 passed / 1 skipped: the DynamoDB-item secret scan skips on the memory store). Every store-level and `client`-fixture API test runs against memory AND moto-mocked DynamoDB.
- **MOCKED vs REAL:** all DynamoDB/Lambda results above are against `moto` (an in-process AWS emulator), plus one live `uvicorn` run against `moto.server`. **No real AWS verification has been performed.**
- Concurrency (mocked DynamoDB, threads): exactly one winner among 16 racing run transitions; 25 concurrent `reserve_tool_call` return exactly `0..24`; 20 concurrent `append_event` produce gap-free `seq 1..20`; 12 concurrent authorizations with `max_tool_calls=5` yield exactly 5 allows and 7 `TOOL_CALL_LIMIT_EXCEEDED` with 12 gap-free events. Multi-process / real-network contention untested.
- Lambda: allow/deny/stop/replay, version mismatch, inactive contract, expiry-before-denial, call limit, unknown run, malformed/base64/proxy events, store failure ⇒ deny, misconfigured ⇒ deny, no Bedrock import, audit log has no contents.
- Live: real `uvicorn` on memory (`:8787`) and on moto-DynamoDB (`:8788`): allow → `.env` deny (run → blocked) → `RUN_NOT_ACTIVE`, identical event sequences.
- `cfn-lint infrastructure/control-plane/template.yaml` → clean. `build_lambda.sh` builds a 2.7 MB zip containing only `intentlock_api/{__init__,store,dynamo_store,service,lambda_handler}.py`, `intentlock_policy`, pydantic (no `app.py`, no `bedrock.py`, no fastapi). Zip was not executed (linux wheels).
- Frontend unchanged: typecheck, 8 vitest, build all pass.
- **Cross-workstream (real):** checked out Person 2 `origin/codex/person2-execution-plane@8a93b37` read-only in a temporary worktree and ran `INTENTLOCK_PERSON1_CHECKOUT=<clean checkout of origin/Ali-Person-1@8064936> python3 -B -m pytest -q tests/sandbox` → **15 passed** (their MCP stdio subprocess → my real FastAPI on a localhost port: ALLOW, PROTECTED_PATH, RUN_NOT_ACTIVE; stop terminates a real subprocess). This is a synthetic harness, NOT live Kiro/model traffic. Worktrees removed afterwards; nothing of theirs modified.

Milestone 4 results (2026-11-07), run in the project venv (`.venv`):
- `.venv/bin/python -m pytest -q` → **234 passed, 7 skipped** (contract 29, policy 83, API+store+Lambda+launcher 122 passed / 1 skipped, integration all skipped without `INTENTLOCK_PERSON2_CHECKOUT`). Frontend: `tsc --noEmit` clean, 8 vitest, `vite build` OK.
- **SYNTHETIC (stand-in processes, no Kiro, no LLM):** `apps/api/tests/test_launcher.py` (launch argv/env, stop → SIGINT, stubborn runner → SIGINT/SIGTERM/SIGKILL, blocked run terminates runner, exit 0 → completed / 3 → failed, runner-reported state not overwritten, spawn failure → failed + 502) on memory AND moto-mocked DynamoDB.
- **REAL Person 2 code + SYNTHETIC agent brain:** `INTENTLOCK_PERSON2_CHECKOUT=<checkout of origin/codex/person2-execution-plane@8a93b37> pytest tests/integration` → 4 passed, 1 xpassed (the supervisor-defect test, intermittent), 1 skipped (REAL Kiro test). Chain: API launch endpoint → my launcher → Person 2's real `launch_kiro.py` + supervisor → a `kiro-cli` stand-in that reads the real generated profile and starts Person 2's real MCP server over stdio and sends scripted `tools/call` → my real `/internal/authorize`. Verified: allowed read reaches the file tool (`isError:false`, content withheld from the events); `.env` → `PROTECTED_PATH` (run `blocked`, denied calls returned only the reason code); replay → `RUN_NOT_ACTIVE`; supervisor registered the PID; SSE events identical (seq and event_id) to the list endpoint; clean exit → run `completed`; stop → state committed first, agent AND MCP server processes gone (one observation: 0.12 s; not an SLA); runner "Not logged in" failure → run `failed` with the intended log line.
- **Dashboard in a real browser** (real API on :8787 + Vite on :5173, Person 2's real runner, synthetic kiro stand-in, `env -i` API process): fixture draft → Approve and launch → Active Run showed launcher RUNNING, agent PID registered by their supervisor, and the allowed MCP read live; Stop → STOPPED, launcher EXITED (code 1 — that run hit the intermittent Person 2 defect above), no agent/MCP processes left, events matched the backend.
- **Cross-workstream (Person 2's own suite on my branch `c9fbf48`, run via a clean worktree with `INTENTLOCK_PERSON1_CHECKOUT`):** `tests/sandbox` → **15 passed** (incl. their 2 real-HTTP MCP→API tests). Full repo run with their checkout available: **238 passed, 2 skipped (real-Kiro test; memory-store secret scan), 1 xpassed**.
- NOT verified: anything with real Kiro (not installed here; Person 2 reports it is not logged in), safe/unsafe PATCH handling end-to-end (no sandbox/impact/promotion exists yet), real AWS.

Milestone 5 results (2026-11-07), project venv:
- Whole repo with Person 2's checkout available: **299 passed, 2 skipped (real-Kiro test; memory-store secret scan), 1 xpassed**. Per suite: contract 29 passed; policy 84 passed; API+store+Lambda+launcher+impact 182 passed / 1 skipped (memory-store secret scan). Each store-level test and each `client`/`env`-fixture API test runs on memory AND moto-mocked DynamoDB. Frontend: `tsc --noEmit` clean, 8 vitest, `vite build` OK. `cfn-lint` on the template clean.
- **Person 2's suite on this branch** (`origin/kavinperson2branch@3847418` checked out read-only in a worktree, `INTENTLOCK_PERSON1_CHECKOUT` = clean checkout of this branch): 15 passed. **My launch e2e against their branch:** 4 passed, 1 xpassed, 1 skipped (real Kiro).
- **Impact/review/promotion (SIMULATED data):** 40 API tests (reports are fixtures; patch artifacts are files written by the test; promoter is a fake that records calls, plus a real localhost HTTP server for the client). Proven: blocked is never approvable (UI disabled AND a direct backend call returns `409 IMPACT_BLOCKED`); wrong digest/base commit refused; reviewer allowlist enforced (403); one atomic decision per report; approval needs a verified artifact, active contract, eligible run; tampered/missing artifact never served/approved/promoted; `pass` never promotes (promoter call count 0 until `/promote`); promotion requires approval + exact digest; at most one promotion request; promoter refusal/outage/mismatched-claim recorded honestly; events/DynamoDB items contain no patch text; redirects/malformed promoter replies/remote plaintext http rejected.
- **Manual browser run (real API + real endpoints; SIMULATED impact reports and a SIMULATED promoter stub):** blocked report → buttons disabled with reason, direct approve call refused 409; passing report → Approve → review recorded (reviewer, contract v1, base, digest) → Request promotion → PROMOTED; Final Audit showed the review and promotion records.
- **Real Kiro:** NOT run by me (Kiro not installed here). Person 2's own report of real Kiro traffic is against commit `1bfd0ff`.
- **AWS (read-only, us-east-1):** see `infrastructure/control-plane/README.md`. Nothing created.

Update 2026-11-07 (late):
- `.venv/bin/python -m pytest` → **301 passed, 7 skipped** in one normal run (7 skips: 6 integration tests that need `INTENTLOCK_PERSON2_CHECKOUT` + real-Kiro test, and the memory-store secret scan). Frontend unchanged (tsc/vitest/build last verified at `055ebb9`).
- **Tests can no longer reach real AWS:** `apps/api/tests/conftest.py` forces fake static credentials, empty config/credentials files and disabled instance-metadata for the whole session (unless `INTENTLOCK_AWS_TEST_TABLE` is explicitly set); `test_no_real_aws.py` asserts credentials resolve to the fakes and no profiles are visible. The `AgentLauncher` now has `shutdown()` (kills runners, joins watcher threads; watchers never touch the store after it), and the launcher tests/e2e call it before the mocked store is torn down. Reason: a watcher thread that outlived its moto mock could otherwise call boto3 with ambient credentials.
- **UNRESOLVED intermittent failure:** `apps/api/tests/test_api.py::test_tool_limit_is_exact_under_concurrent_requests[dynamodb-moto]` (12 threads call `service.authorize_request` on mocked DynamoDB with `max_tool_calls=5`; expects exactly 5 allows, 7 `TOOL_CALL_LIMIT_EXCEEDED`, 12 gap-free events). Observed failing about 1 run in 9–10 when run together with `test_launcher.py`/the full suite (3 failures seen overall, one of which had an extra pytest warning, possibly an unhandled thread exception — unconfirmed); 0 failures in 25 isolated runs, 0 in 40 iterations of a standalone stress script, and none in the last full runs. **The failure message was never captured** (output lost twice), so the root cause is unidentified. Suspects: `append_event` transaction-conflict retries exhausting under CPU load (raises `StoreError`, i.e. fails closed), moto thread-safety, or interference from other threads. Evidence of over-allowing: none (every exception path in the service denies/raises; the test asserts exactness). I judged it not security-critical and stopped investigating by owner instruction; the diagnostics scripts are preserved in the session scratchpad. The test now includes thread exceptions and counts in its assertion message so the next occurrence explains itself. If it recurs, capture with `-p no:cacheprovider --tb=long -W error::pytest.PytestUnhandledThreadExceptionWarning`.

## 12. Known Issues and Blockers
- **No dashboard→real-Kiro run yet.** Kiro is not installed/authenticated on this machine; the procedure and an opt-in test exist (`tests/integration/README.md`). Person 2's real-Kiro result predates the launcher integration.
- **TwinGuard/promoter do not exist** (Person 2): impact reports, patches and promotion in my tests are simulated. Real exact-patch promotion, safe/unsafe patch handling and `TRUSTED_TEST_MODIFIED` enforcement are unverified end to end.
- Person 2 issue tracker (7 items) in §9; items 1, 2, 3, 5 are open/unverified and affect stop reliability and the claim that all Kiro access is mediated.
- Review authorization is an env allowlist of self-asserted names behind an unauthenticated public API (demo). Production needs real authentication (e.g. Cognito/IAM) before reviewer identity means anything.
- Artifact reader is a LOCAL directory stand-in; no S3 reader/writer exists. The launcher is LOCAL-DEV only (subprocess on the API host).
- **No real-AWS verification** of DynamoDB, Lambda, IAM or Bedrock; nothing deployed; Lambda account concurrency limit is 10; Bedrock model access is unverified (listing succeeded; invoke not tried).
- Contract `status` is mutable by design; run expiry is lazy; stop-vs-tool TOCTOU window cannot be fully closed by the control plane.
- Trusted-test patterns are tuned for the frozen TypeScript/Node scope.
- Disk pressure: the dev machine's volume was at 99% during this session (a transient ENOSPC aborted one command). `.venv` (~280 MB) and `apps/web/node_modules` (~70 MB) are regenerable.
- `intentlock-prd.md` is not in the repo (Person 2 gitignored their local copy).
- **AWS access is unverified** until the `intentlock` profile exists; no deploy is possible before that. The exposed-key rotation (Kavin) must happen first.
- Public API is unauthenticated (see README list); do not expose beyond localhost; Bedrock proposal endpoint has no rate limit.
- Template has no S3 bucket; the Lambda zip bucket and Bedrock model id are open decisions.

## 13. Decisions Needed
**Owner (Ali/Kavin):**
1. Merge `origin/codex/ali-review-safeguards` into `Ali-Person-1`? (reviewed, tests pass, trivial docs conflict; changes reviewer authentication and makes stopped runs un-approvable.) Not merged.
2. Bedrock model id; Lambda-zip bucket name; $25 budget alert; explicit approval to deploy.
3. Confirm the shared-account ownership/naming proposal with Person 2.
**Person 2:** confirm the TwinGuard contract (§9: `/internal/impact`, artifact location, `/promote`, `TRUSTED_TEST_MODIFIED`); fix/confirm the open runner items; promoter must recheck run state before applying (an in-flight promotion cannot be cancelled by the control plane); replace `apps/mcp-server/schemas` copies with canonical loading.

## 14. Next Steps
1. **Owner:** create the `intentlock` profile privately; then identity check and `LIST_MODELS=1 CODE_BUCKET=<name> PROFILE=intentlock EXPECTED_USER=aparpia EXPECTED_ACCOUNT=247495661854 infrastructure/control-plane/verify_aws_readonly.sh` (I run it once the profile exists).
2. **Owner:** decide on the safeguards-branch merge and the deployment decisions in §13.
3. Integrate Person 2's TwinGuard when it exists (contract in §9); until then nothing of the sandbox is duplicated here.
4. Kavin: all Kiro work and the real-Kiro run on the combined app.

