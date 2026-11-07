"""IntentLock control-plane API (FastAPI). Local dev: uvicorn intentlock_api.app:create_app --factory"""
import asyncio
import hmac
import json
import logging
import os
import uuid
from typing import Any, Callable

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from intentlock_policy import SCHEMA_VERSION, authorize
from intentlock_policy.models import MissionContract, MissionContractDraft, Run
from intentlock_policy.redact import redact_arguments, sanitize_details

from .bedrock import ProposalError, bedrock_invoker, propose_draft
from .store import LocalMemoryStore, now

audit_log = logging.getLogger("intentlock.audit")
TERMINAL = {"stopped", "blocked", "completed", "failed"}


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=2000)
    repository_id: str = "intentlock-demo"
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class CreateContractBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: MissionContractDraft
    approved: bool  # explicit human approval flag; must be true
    approved_by: str = Field(min_length=1, max_length=128)
    contract_id: str | None = None  # supply to create a NEW VERSION of an existing contract


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_id: str
    contract_version: int = Field(ge=1)


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="stopped by user", max_length=200)


class AgentInfoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_process_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    sandbox_id: str | None = Field(default=None, max_length=128)


class IngestEventBody(BaseModel):
    """Execution-plane event ingestion. Details are sanitised; never send contents/patches."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    event_type: str = Field(pattern=r"^(agent_event|impact_report|review_decision|promotion|run_completed)$")
    message: str | None = Field(default=None, max_length=500)
    request_id: str | None = Field(default=None, max_length=128)
    details: dict[str, Any] | None = None


def create_app(store: LocalMemoryStore | None = None,
               invoker: Callable[[str, str], str] | None = None,
               internal_token: str | None = None) -> FastAPI:
    store = store or LocalMemoryStore()
    token = internal_token if internal_token is not None else os.environ.get("INTENTLOCK_INTERNAL_TOKEN", "")
    app = FastAPI(title="IntentLock control plane", version=SCHEMA_VERSION)
    origins = os.environ.get("INTENTLOCK_CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def require_internal(x_intentlock_internal_token: str | None) -> None:
        # Fail closed: an unconfigured token disables the internal API entirely.
        if not token:
            raise HTTPException(503, "INTERNAL_AUTH_NOT_CONFIGURED")
        if not x_intentlock_internal_token or not hmac.compare_digest(x_intentlock_internal_token, token):
            raise HTTPException(401, "INVALID_INTERNAL_TOKEN")

    def expire_if_needed(run_id: str) -> Run | None:
        """Mark an overdue active run inactive (and record why) BEFORE anything else
        looks at it, so state and denials agree. No background reaper: lazy on access."""
        with store.lock:
            run = store.get_run(run_id)
            if run is None or run.status != "active" or run.started_at is None:
                return run
            c = store.get_contract(run.contract_id, run.contract_version)
            if c is None:
                return run
            if (now() - run.started_at).total_seconds() >= c.limits.run_timeout_seconds:
                run = store.update_run(run_id, status="stopped", stopped_at=now(), stop_reason="run_timeout")
                store.append_event(run_id, event_type="run_stopped", status="recorded",
                                   message="Run time limit reached",
                                   details={"status": "stopped", "cause": "run_timeout",
                                            "run_timeout_seconds": c.limits.run_timeout_seconds})
            return run

    def get_run_or_404(run_id: str) -> Run:
        r = expire_if_needed(run_id)
        if not r:
            raise HTTPException(404, "RUN_NOT_FOUND")
        return r

    def stop_run_state(run_id: str, status: str, reason: str) -> Run:
        """Commit the state change (and its event) BEFORE anything reports success."""
        with store.lock:
            run = store.get_run(run_id)
            if run is None or run.status in TERMINAL:
                return run  # type: ignore[return-value]
            run = store.update_run(run_id, status=status, stopped_at=now(), stop_reason=reason)
            store.append_event(run_id, event_type="run_stopped", status="recorded",
                               message=reason, details={"status": status})
            return run

    # ------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "schema_version": SCHEMA_VERSION, "store": store.backend}

    # --------------------------------------------------------- contracts
    @app.post("/api/contracts/propose")
    def propose(body: ProposeBody) -> dict:
        try:
            inv = invoker or bedrock_invoker()
            draft, attempts = propose_draft(body.task, body.repository_id, body.base_commit, inv)
        except ProposalError as e:
            raise HTTPException(502, f"CONTRACT_PROPOSAL_FAILED: {e}") from e
        except Exception as e:  # missing creds, throttling, etc. -> no contract is created
            raise HTTPException(503, f"BEDROCK_UNAVAILABLE: {type(e).__name__}") from e
        return {"draft": draft.model_dump(mode="json"), "attempts": attempts,
                "status": "proposed", "requires_human_approval": True}

    @app.post("/api/contracts", status_code=201)
    def create_contract(body: CreateContractBody) -> dict:
        if body.approved is not True:
            raise HTTPException(400, "APPROVAL_REQUIRED")
        with store.lock:
            cid = body.contract_id or "contract_" + uuid.uuid4().hex[:12]
            prev = store.latest_version(cid)
            if body.contract_id and prev == 0:
                raise HTTPException(404, "CONTRACT_NOT_FOUND")
            c = MissionContract(**body.draft.model_dump(), contract_id=cid, version=prev + 1,
                                created_by=body.approved_by, approved_at=now(), status="active")
            if prev:
                store.set_contract_status(cid, prev, "superseded")  # old version stops authorizing
            store.put_contract(c)
        return c.model_dump(mode="json")

    @app.get("/api/contracts/{contract_id}/versions/{version}")
    def get_contract(contract_id: str, version: int) -> dict:
        c = store.get_contract(contract_id, version)
        if not c:
            raise HTTPException(404, "CONTRACT_NOT_FOUND")
        return c.model_dump(mode="json")

    # -------------------------------------------------------------- runs
    @app.post("/api/runs", status_code=201)
    def create_run(body: CreateRunBody) -> dict:
        c = store.get_contract(body.contract_id, body.contract_version)
        if not c:
            raise HTTPException(404, "CONTRACT_NOT_FOUND")
        if c.status != "active":
            raise HTTPException(409, "CONTRACT_NOT_ACTIVE")
        t = now()
        run = Run(run_id="run_" + uuid.uuid4().hex[:12], contract_id=c.contract_id,
                  contract_version=c.version, status="active", base_commit=c.base_commit,
                  created_at=t, started_at=t)
        with store.lock:
            store.put_run(run)
            store.append_event(run.run_id, event_type="run_started", status="recorded",
                               message="Run started", details={"contract_id": c.contract_id,
                                                               "contract_version": c.version})
        return run.model_dump(mode="json")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return get_run_or_404(run_id).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str, body: StopBody | None = None) -> dict:
        get_run_or_404(run_id)
        run = stop_run_state(run_id, "stopped", (body.reason if body else "stopped by user"))
        # State is committed above. Process termination is performed by the execution
        # plane (it sees stop_run / RUN_NOT_ACTIVE and run status) - not claimed here.
        return {"run": run.model_dump(mode="json"), "state_committed": True,
                "process_termination": "delegated_to_execution_plane"}

    @app.get("/api/runs/{run_id}/events")
    def list_events(run_id: str, after_seq: int = Query(0, ge=0)) -> dict:
        get_run_or_404(run_id)
        return {"events": [e.model_dump(mode="json") for e in store.list_events(run_id, after_seq)]}

    @app.get("/api/runs/{run_id}/events/stream")
    async def stream_events(run_id: str, after_seq: int = Query(0, ge=0),
                            timeout_seconds: float = Query(300, gt=0, le=3600),
                            last_event_id: str | None = Header(default=None)):
        get_run_or_404(run_id)
        resume = after_seq
        if last_event_id and last_event_id.isdigit():  # browser EventSource reconnect
            resume = max(resume, int(last_event_id))

        async def gen():
            last, waited = resume, 0.0
            while waited < timeout_seconds:
                expire_if_needed(run_id)
                for e in store.list_events(run_id, last):
                    last = e.seq
                    yield f"id: {e.seq}\nevent: {e.event_type}\ndata: {e.model_dump_json()}\n\n"
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)
                waited += 0.25

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # ---------------------------------------------------------- internal
    @app.post("/internal/authorize")
    def internal_authorize(body: Any = Body(...),
                           x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        require_internal(x_intentlock_internal_token)
        raw = body if isinstance(body, dict) else None
        run_id = raw.get("run_id") if raw else None
        with store.lock:  # serialises call counting + stop transition (single process)
            run = expire_if_needed(run_id) if isinstance(run_id, str) else None
            contract = store.get_contract(run.contract_id, run.contract_version) if run else None
            prior = store.count_tool_requests(run.run_id) if run else 0
            ev = None
            if run is not None:  # event is created BEFORE authorization (FR-4)
                tool = raw.get("tool") if isinstance(raw.get("tool"), str) else None
                try:
                    red = redact_arguments(tool or "", raw.get("arguments"))
                except Exception:
                    red = {"_invalid": True}
                rid = raw.get("request_id") if isinstance(raw.get("request_id"), str) else None
                ev = store.append_event(run.run_id, event_type="tool_request", status="pending",
                                        request_id=rid, tool=tool, arguments_redacted=red)
            decision = authorize(raw if raw is not None else {}, run, contract, prior)
            if ev is not None:
                store.update_event(run.run_id, ev.seq, status="decided", decision=decision.decision,
                                   reason_code=decision.reason_code, message=decision.message,
                                   latency_ms=decision.latency_ms, evidence=decision.evidence)
                if decision.stop_run:
                    stop_run_state(run.run_id, "blocked", f"{decision.reason_code}: request {ev.request_id}")
        audit_log.info(json.dumps({"event": "authorization", "request_id": decision.request_id,
                                   "run_id": run_id if isinstance(run_id, str) else None,
                                   "decision": decision.decision, "reason_code": decision.reason_code,
                                   "stop_run": decision.stop_run, "latency_ms": decision.latency_ms}))
        return decision.model_dump(mode="json")

    @app.post("/internal/runs/{run_id}/agent")
    def set_agent_info(run_id: str, body: AgentInfoBody,
                       x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        require_internal(x_intentlock_internal_token)
        get_run_or_404(run_id)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        return store.update_run(run_id, **fields).model_dump(mode="json")

    @app.post("/internal/events", status_code=201)
    def ingest_event(body: IngestEventBody,
                     x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        require_internal(x_intentlock_internal_token)
        get_run_or_404(body.run_id)
        ev = store.append_event(body.run_id, event_type=body.event_type, status="recorded",
                                request_id=body.request_id, message=body.message,
                                details=sanitize_details(body.details) if body.details else None)
        return ev.model_dump(mode="json")

    return app
