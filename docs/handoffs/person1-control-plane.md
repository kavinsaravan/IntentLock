# Handoff — Person 1 (Control Plane)

## 1. Project and Workstream
- Product: IntentLock with TwinGuard (A10 Agent Harness hackathon)
- Owner: Person 1 (Ali). Workstream A: dashboard, API, Bedrock proposal, contract validation/approval/versioning, deterministic policy, run state/stop, events, DynamoDB/CloudWatch/infra, frontend integration.
- PRD: `intentlock-prd.md` (sections 9–12, 15, 17).

## 2. Last Updated
- UTC: 2026-11-07
- Branch: `Ali-Person-1` (the PRD calls it `person1-control-plane`; actual name is `Ali-Person-1`)
- Commit: `8064936` (milestone 3: DynamoDB store, Lambda handler, infra; earlier: `57d3c5c` dashboard, `a66614d` foundation)
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
| Impact/approve/promote UI | Only exercised against a throwaway stub; backend endpoints do not exist |
| Execution-plane integration | **Tested locally, cross-workstream:** Person 2's `tests/sandbox` (15 tests incl. 2 real localhost HTTP MCP→API tests) pass against this branch at `8064936` (memory store). Not yet re-run on DynamoDB mode; no live Kiro/model traffic (their Kiro is not logged in) |

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

## 5. Current Implementation Work
Dashboard is functional against the local API. Next per instruction: integrate Person 2's MCP traffic, then DynamoDB, Lambda, CloudWatch, real Bedrock verification. Nothing half-finished in the tree.

## 6. Files Created or Modified
`AGENTS.md` (shared root file — new; Person 2 must review/integrate), `.gitignore`, `pytest.ini`, `packages/contracts/**` (shared dir, proposed), `packages/policy-engine/**`, `apps/api/**`, `tests/contract/**`, `tests/policy/**`, this handoff. Milestone 2 added `apps/web/**`, small `apps/api` changes (lazy run expiry, `Last-Event-ID`), extra tests. `.gitignore` gained `dist/`. `packages/event-model/` is an empty placeholder (Event schema lives in `packages/contracts`).

Milestone 3: `apps/api/intentlock_api/{store,dynamo_store,service,lambda_handler,app}.py` (store refactor, new modules), `apps/api/tests/{conftest,test_store_contract,test_lambda_handler,test_api}.py`, `infrastructure/control-plane/{template.yaml,build_lambda.sh,README.md}`, `.gitignore` (`infrastructure/control-plane/build/`), this handoff.

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

## 10. AWS Configuration
| Service | State |
|---|---|
| DynamoDB | **Locally simulated (moto) and tested there. NOT verified on real AWS.** Template written, not deployed. A real-table check exists (`INTENTLOCK_AWS_TEST_TABLE=<table> pytest apps/api/tests/test_store_contract.py`) but has NOT been run |
| Lambda | Handler implemented and tested locally with mocked DynamoDB; zip builds; **not deployed, not invoked on AWS** |
| IAM | Least-privilege roles/policies in `template.yaml` (table-only GetItem/PutItem/UpdateItem/Query; log-group-only logs; no Bedrock); `cfn-lint` clean; **never applied** |
| CloudWatch | Not configured (log group is created by the template when deployed); metrics not implemented |
| Bedrock | Code only; not tested against AWS |
| Amplify / AgentCore / S3 | Not started (Person 2 owns S3/AgentCore) |
Nothing was deployed. The shell has ambient AWS credentials for some account; no AWS API call was made by any test (moto tests force fake credentials). Deploying needs your confirmation of account, region, and stack target. No credentials are stored in the repo.

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

## 12. Known Issues and Blockers
- **No real-AWS verification** of DynamoDB, Lambda, IAM, or Bedrock (needs account/region/target confirmation from you).
- moto does not model throttling/latency/true transaction contention; `append_event` retries up to 60 times on transaction conflicts then raises `StoreError` (fails closed).
- Contract `status` is mutable by design (active→superseded); everything else in a version is immutable. `set_contract_status` is not conditional on the old status.
- Run expiry is lazy (applied on access: authorize, GET run/events, SSE loop), no background reaper.
- A stop landing after the final re-check but before the tool runs cannot be prevented by the control plane (inherent TOCTOU); the execution plane also polls run state.
- Event details/`/internal/events` are sanitised heuristically; callers must not send contents/patches.
- Public API has no authentication (hackathon demo). Dashboard: draft not persisted across reload; impact UI only verified against a stub.
- `pip install` for boto3/moto/cfn-lint upgraded packages in the shared Anaconda environment on this machine (dependency warnings for streamlit/aiobotocore). Use a venv for further work.
- `intentlock-prd.md` is not in the repo (shared root file; whoever adds it should tell the other).

## 13. Decisions Needed From Person 2
1. Runner interface so the dashboard launch can start the Kiro/MCP agent for a `run_id` (§9 item 3).
2. Who serves `/api/impact/*` (§9 earlier interface list) and whether control plane proxies it.
3. Trusted test-catalogue rule so agent-editable files cannot weaken regression tests (§9 item 4).
4. When ready: replace `apps/mcp-server/schemas` copies with canonical `packages/contracts` loading; merge strategy (PRs into `main`) — I have NOT merged anything.

## 14. Next Steps
1. Run the real cross-workstream MCP → `/internal/authorize` test (Person 2's branch exists) and fix any mismatch.
2. With your confirmation of AWS account/region/stack: deploy `template.yaml`, run the real-table store tests, invoke the Lambda for real, then CloudWatch metrics.
3. Real Bedrock verification (model access + `INTENTLOCK_BEDROCK_MODEL_ID`), then the runner/launch wiring and impact endpoints once Person 2 serves them.

