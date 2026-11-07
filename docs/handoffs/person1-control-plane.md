# Handoff — Person 1 (Control Plane)

## 1. Project and Workstream
- Product: IntentLock with TwinGuard (A10 Agent Harness hackathon)
- Owner: Person 1 (Ali). Workstream A: dashboard, API, Bedrock proposal, contract validation/approval/versioning, deterministic policy, run state/stop, events, DynamoDB/CloudWatch/infra, frontend integration.
- PRD: `intentlock-prd.md` (sections 9–12, 15, 17).

## 2. Last Updated
- UTC: 2026-11-07
- Branch: `Ali-Person-1` (the PRD calls it `person1-control-plane`; actual name is `Ali-Person-1`)
- Commit: see `git log -1 -- docs/handoffs/person1-control-plane.md` (hash recorded at bottom after commit)
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
| React dashboard (5 screens) | Not started |
| Execution-plane integration | Not started — waiting on Person 2 |

## 4. Completed Features
- **Canonical schemas** — `packages/contracts/schemas/*.schema.json`, fixtures in `packages/contracts/fixtures/`, models in `packages/policy-engine/intentlock_policy/models.py`. Tested locally.
- **Deterministic policy** — `packages/policy-engine/intentlock_policy/engine.py`, `paths.py`. Pure function, fail-closed, 12-step order from PRD §12. Tested locally.
- **Redaction** — `.../redact.py`: events store only redacted args (patch → sha256 + byte count). Tested locally.
- **API** — `apps/api/intentlock_api/{app,store,bedrock}.py`. Tested locally via ASGI TestClient and one live `uvicorn` + `curl` smoke run (allow → deny `.env` → `RUN_NOT_ACTIVE`).

## 5. Current Implementation Work
Next: DynamoDB store + Lambda handler, CloudWatch metrics, then dashboard. Nothing half-finished in the tree.

## 6. Files Created or Modified
`AGENTS.md` (shared root file — new; Person 2 must review/integrate), `.gitignore`, `pytest.ini`, `packages/contracts/**` (shared dir, proposed), `packages/policy-engine/**`, `apps/api/**`, `tests/contract/**`, `tests/policy/**`, this handoff. `packages/event-model/` is an empty placeholder (Event schema lives in `packages/contracts`).

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
6. `request_permission` and `finish_task` are always allowed for an active run (no repo effect) even if absent from `allowed_tools`; still argument-validated and counted.

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
- Blockers: none known; waiting on Person 2's branch/handoff (not present on `origin` as of this update).

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
- `python3 -m pytest -q` → **111 passed** (contract 36-ish, policy ~50, API 17). Executed 2026-11-07.
- Covered: all PRD §11-A acceptance items except "dashboard displays real events" (no dashboard yet) and CloudWatch logging checks; includes traversal/encoded/absolute paths, hidden patch headers, protected precedence, version mismatch, inactive/stopped, call/time limits, fail-closed on internal error, no secret/patch leakage in decisions/events/audit log, SSE.
- Manual: live `uvicorn` + `curl`: allow → `.env` deny (run → blocked) → `RUN_NOT_ACTIVE`. Latency ≈0.01–0.35 ms in-process (not an end-to-end/AWS figure).
- Missing: DynamoDB/Lambda/Bedrock integration tests, concurrency across processes (current counting uses a single-process lock), cross-workstream integration.

## 12. Known Issues and Blockers
- In-memory store: restart loses everything; tool-call counting is only race-free in one process (DynamoDB version needs an atomic counter).
- Run time limit only enforced when a request arrives (no background reaper).
- Public API has no authentication (hackathon demo).
- Person 2 branch/handoff unavailable.

## 13. Decisions Needed From Person 2
Accept/modify the 0.1.0 deviations in §8; MCP→authorize transport (HTTP + token as above vs Lambda invoke); who calls `/internal/events` for impact/promotion; AgentCore interceptor payload mapping to ToolRequest; actual branch name.

## 14. Next Steps
1. Person 2 confirms schema 0.1.0 and wires MCP server → `/internal/authorize` (first shared checkpoint).
2. DynamoDB store + Lambda handler + CloudWatch metrics (+ `infrastructure/control-plane`).
3. React dashboard (Create Mission → Review → Active Run → Impact → Audit) over the real API.
