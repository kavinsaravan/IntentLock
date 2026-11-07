# Handoff — Person 1 (Control Plane)

## 1. Project and Workstream
- Product: IntentLock with TwinGuard (A10 Agent Harness hackathon)
- Owner: Person 1 (Ali). Workstream A: dashboard, API, Bedrock proposal, contract validation/approval/versioning, deterministic policy, run state/stop, events, DynamoDB/CloudWatch/infra, frontend integration.
- PRD: `intentlock-prd.md` (sections 9–12, 15, 17).

## 2. Last Updated
- UTC: 2026-11-07
- Branch: `Ali-Person-1` (the PRD calls it `person1-control-plane`; actual name is `Ali-Person-1`)
- Commit: `a66614d` (milestone 1: schemas, policy engine, API; later commits may follow)
- Schema version: `0.1.0` (PROPOSED — not yet agreed with Person 2)

## 3. Current Status
| Area | State |
|---|---|
| Shared schemas + fixtures (0.1.0) | Tested locally |
| Policy engine (PRD §12) | Tested locally |
| Backend API (contracts, runs, stop, events, SSE, authorize, ingestion) | Tested locally (in-memory store) |
| Bedrock contract proposal | Implemented but untested against real AWS (unit-tested with fake invoker) |
| Lambda authorization handler | Not started |
| DynamoDB persistence | Not started (in-memory local fallback only) |
| CloudWatch logs/metrics | Partially: structured audit log line only; no metrics |
| Infra (`infrastructure/control-plane`) | Not started |
| React dashboard (5 screens) | Tested locally (manual browser run against real local API; unit tests for pure logic; no component tests) |
| Impact/approve/promote UI | Implemented; only exercised against a throwaway stub (see §11). Backend endpoints do not exist yet |
| Execution-plane integration | Not started — waiting on Person 2 |

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

## 5. Current Implementation Work
Dashboard is functional against the local API. Next per instruction: integrate Person 2's MCP traffic, then DynamoDB, Lambda, CloudWatch, real Bedrock verification. Nothing half-finished in the tree.

## 6. Files Created or Modified
`AGENTS.md` (shared root file — new; Person 2 must review/integrate), `.gitignore`, `pytest.ini`, `packages/contracts/**` (shared dir, proposed), `packages/policy-engine/**`, `apps/api/**`, `tests/contract/**`, `tests/policy/**`, this handoff. Milestone 2 added `apps/web/**`, small `apps/api` changes (lazy run expiry, `Last-Event-ID`), extra tests. `.gitignore` gained `dist/`. `packages/event-model/` is an empty placeholder (Event schema lives in `packages/contracts`).

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

## 10. AWS Configuration
| Service | State |
|---|---|
| Bedrock | Code written, **not tested against AWS**; fake-invoker unit tests only |
| DynamoDB | Not configured (in-memory fallback, clearly labelled in `/health`) |
| Lambda | Not started (policy engine is Lambda-compatible: pure function, no I/O) |
| CloudWatch | Not configured; structured audit log line only (no contents) |
| Amplify / AgentCore | Not started / Person 2 |
No credentials are stored in the repo.

## 11. Tests and Verification
- `python3 -m pytest -q` → **116 passed** (contract 29, policy 66, API 21). Executed 2026-11-07. Added: expiry-before-denial (incl. invalid-args request on expired run), plain-GET expiry, `Last-Event-ID` resume, `request_permission`/`finish_task` conditionality (run state, contract state/version, limits, arguments).
- Frontend: `cd apps/web && npx tsc --noEmit` clean; `npx vitest run` → 8 passed (event merge/dedupe, decision counts, latency percentiles, impact-id discovery, diff parser, error formatting); `npx vite build` OK. No component/E2E tests.
- Manual browser run (built-in browser, real API on :8787 + Vite on :5173): Bedrock failure shown honestly (`503 BEDROCK_UNAVAILABLE: ModuleNotFoundError` — boto3 not installed here); fixture draft → review → approve+launch → Active Run with live SSE; curl-simulated execution plane produced allow, allow, `.env` deny (run → BLOCKED, banner, stop reason), `RUN_NOT_ACTIVE`; agent id set via `/internal/runs/{id}/agent` appeared; stop button on a second run committed `stopped` and displayed backend state; Final Audit rendered real decisions; killing the API showed "API unreachable" and stream/poll error states; `impact_report` event ingestion made the Impact screen request `/api/impact/impact_01` and show "unavailable (404)".
- Impact review loaded state (block report with failed test + diff; pass report with approve → promote gating) was verified ONLY against a throwaway stub I ran outside the repo that serves the committed fixture. That proves the UI, not any real TwinGuard behaviour.
- Covered: all PRD §11-A acceptance items except "dashboard displays real events" (no dashboard yet) and CloudWatch logging checks; includes traversal/encoded/absolute paths, hidden patch headers, protected precedence, version mismatch, inactive/stopped, call/time limits, fail-closed on internal error, no secret/patch leakage in decisions/events/audit log, SSE.
- Manual: live `uvicorn` + `curl`: allow → `.env` deny (run → blocked) → `RUN_NOT_ACTIVE`. Latency ≈0.01–0.35 ms in-process (not an end-to-end/AWS figure).
- Missing: component/E2E tests for the dashboard; DynamoDB/Lambda/Bedrock integration tests, concurrency across processes (current counting uses a single-process lock), cross-workstream integration.

## 12. Known Issues and Blockers
- In-memory store: restart loses everything; tool-call counting is only race-free in one process (DynamoDB version needs an atomic counter).
- Run expiry is lazy (applied on access), not a background reaper; the dashboard polls every 2s so it shows expiry within ~2s of the limit while open.
- Dashboard: Bedrock-less env shows an error (expected) and offers the fixture path; `Contract` screen keeps the draft only in memory (reload loses it); `/internal/*` is not proxied by Vite (by design); no auth on public API; polling fetches the full event list every 2s (fine for demo size).
- `intentlock-prd.md` was not added to the repo (shared root file; avoids a conflict with Person 2).
- Public API has no authentication (hackathon demo).
- Person 2 branch/handoff unavailable.

## 13. Decisions Needed From Person 2
Accept/modify the 0.1.0 deviations in §8; MCP→authorize transport (HTTP + token as above vs Lambda invoke); who calls `/internal/events` for impact/promotion; AgentCore interceptor payload mapping to ToolRequest; actual branch name.

## 14. Next Steps
1. Person 2 confirms schema 0.1.0 and wires MCP server → `/internal/authorize` (first shared checkpoint); then read their handoff and reconcile the impact/promote interfaces in §9.
2. DynamoDB store + Lambda handler + CloudWatch metrics (+ `infrastructure/control-plane`).
3. Real Bedrock verification (needs boto3, model access, `INTENTLOCK_BEDROCK_MODEL_ID`).
