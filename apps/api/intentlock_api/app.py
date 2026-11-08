"""IntentLock control-plane API (FastAPI). Local dev: uvicorn intentlock_api.app:create_app --factory"""
import asyncio
import hmac
import os
from contextlib import asynccontextmanager
import uuid
from typing import Any, Callable, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from intentlock_policy import SCHEMA_VERSION
from intentlock_policy.models import MissionContract, MissionContractDraft, Run
from intentlock_policy.redact import sanitize_details

from . import impact as impact_api
from . import service
from .artifacts import LocalDirArtifactReader
from .promoter import PromoterClient
from .launcher import AgentLauncher, AlreadyLaunched, LaunchError, LauncherNotConfigured
from .bedrock import ProposalError, bedrock_invoker, propose_draft
from .store import ContractExists, StoreError, create_store_from_env, now

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


class RunStateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "failed"]
    reason: str | None = Field(default=None, max_length=200)


class IngestEventBody(BaseModel):
    """Execution-plane event ingestion. Details are sanitised; never send contents/patches."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    event_type: str = Field(pattern=r"^(agent_event|impact_report|review_decision|promotion|run_completed)$")
    message: str | None = Field(default=None, max_length=500)
    request_id: str | None = Field(default=None, max_length=128)
    details: dict[str, Any] | None = None


def create_app(store: Any = None, launcher: Any = None, artifacts: Any = "env", promoter: Any = "env",
               reviewers: Any = "env", reviewer_tokens: Any = "env",
               invoker: Callable[[str, str], str] | None = None,
               internal_token: str | None = None) -> FastAPI:
    store = store if store is not None else create_store_from_env()
    launcher = launcher if launcher is not None else AgentLauncher(store)
    artifacts = LocalDirArtifactReader.from_env() if artifacts == "env" else artifacts
    promoter = PromoterClient.from_env() if promoter == "env" else promoter
    reviewers = impact_api.reviewers_from_env() if reviewers == "env" else reviewers
    reviewer_tokens = impact_api.reviewer_tokens_from_env() if reviewer_tokens == "env" else reviewer_tokens
    token = internal_token if internal_token is not None else os.environ.get("INTENTLOCK_INTERNAL_TOKEN", "")
    @asynccontextmanager
    async def lifespan(_app):
        warm = getattr(store, "warm_up", None)       # DynamoDB: open the connection before the first authorization
        if warm is not None:
            await asyncio.to_thread(warm)
        yield

    app = FastAPI(title="IntentLock control plane", version=SCHEMA_VERSION, lifespan=lifespan)
    origins = os.environ.get("INTENTLOCK_CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def require_internal(x_intentlock_internal_token: str | None) -> None:
        # Fail closed: an unconfigured token disables the internal API entirely.
        if not token:
            raise HTTPException(503, "INTERNAL_AUTH_NOT_CONFIGURED")
        if not x_intentlock_internal_token or not hmac.compare_digest(x_intentlock_internal_token, token):
            raise HTTPException(401, "INVALID_INTERNAL_TOKEN")

    def expire_if_needed(run_id: str) -> Run | None:
        return service.expire_if_needed(store, run_id, lambda: now())

    def get_run_or_404(run_id: str) -> Run:
        r = expire_if_needed(run_id)
        if not r:
            raise HTTPException(404, "RUN_NOT_FOUND")
        return r

    def stop_run_state(run_id: str, status: str, reason: str) -> Run | None:
        return service.stop_run_state(store, run_id, status, reason, lambda: now())

    @app.exception_handler(StoreError)
    async def store_error(_req, exc):  # persistence failure => never pretend success
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "STORE_UNAVAILABLE"})

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
        cid = body.contract_id or "contract_" + uuid.uuid4().hex[:12]
        if body.contract_id and store.latest_version(cid) == 0:
            raise HTTPException(404, "CONTRACT_NOT_FOUND")
        for _ in range(10):  # versions are immutable: retry on a lost race, never overwrite
            prev = store.latest_version(cid)
            c = MissionContract(**body.draft.model_dump(), contract_id=cid, version=prev + 1,
                                created_by=body.approved_by, approved_at=now(), status="active")
            try:
                store.put_contract(c)
            except ContractExists:
                continue
            if prev:
                store.set_contract_status(cid, prev, "superseded")  # old version stops authorizing
            return c.model_dump(mode="json")
        raise HTTPException(409, "CONTRACT_VERSION_CONFLICT")

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
        st = launcher.status(run_id)
        return {"run": run.model_dump(mode="json"), "state_committed": True,
                "process_termination": ("launcher_watcher_armed" if st.get("launched") and st.get("alive")
                                        else "launcher_already_exited" if st.get("launched")
                                        else "delegated_to_execution_plane")}

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

    # ------------------------------------------------------- agent launch
    @app.get("/api/launcher")
    def launcher_info() -> dict:
        return launcher.info()

    @app.post("/api/runs/{run_id}/launch", status_code=201)
    def launch_agent(run_id: str) -> dict:
        run = get_run_or_404(run_id)
        if run.status != "active":
            raise HTTPException(409, "RUN_NOT_ACTIVE")
        contract = store.get_contract(run.contract_id, run.contract_version)
        if contract is None or contract.status != "active":
            raise HTTPException(409, "CONTRACT_NOT_ACTIVE")
        try:
            rec = launcher.launch(run, contract)
        except LauncherNotConfigured as e:
            raise HTTPException(503, f"AGENT_LAUNCHER_NOT_CONFIGURED: {e}") from e
        except AlreadyLaunched as e:
            raise HTTPException(409, "ALREADY_LAUNCHED") from e
        except LaunchError as e:
            service.stop_run_state(store, run_id, "failed", f"agent launch failed: {e}", lambda: now())
            raise HTTPException(502, f"AGENT_LAUNCH_FAILED: {e}") from e
        store.append_event(run_id, event_type="agent_event", status="recorded", message="Agent launcher started",
                           details={"launcher_pid": rec.process.pid, "mode": "local-subprocess"})
        return {"launched": True, "launcher_pid": rec.process.pid}

    @app.get("/api/runs/{run_id}/launch")
    def launch_status(run_id: str) -> dict:
        get_run_or_404(run_id)
        return launcher.status(run_id)

    # ---------------------------------------------------------- internal
    @app.post("/internal/authorize")
    def internal_authorize(body: Any = Body(...),
                           x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        require_internal(x_intentlock_internal_token)
        rid = body.get("request_id") if isinstance(body, dict) and isinstance(body.get("request_id"), str) else "unknown"
        try:
            decision = service.authorize_request(body, store, lambda: now())
        except Exception:  # store/infra failure: fail closed with a decision, not a 500
            decision = service.error_decision(rid)
        return decision.model_dump(mode="json")

    @app.post("/internal/runs/{run_id}/agent")
    def set_agent_info(run_id: str, body: AgentInfoBody,
                       x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        require_internal(x_intentlock_internal_token)
        get_run_or_404(run_id)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        return store.update_run(run_id, **fields).model_dump(mode="json")

    @app.post("/internal/runs/{run_id}/state")
    def set_run_state(run_id: str, body: RunStateBody,
                      x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        """Execution plane reports completion/failure. Atomic active->terminal; first writer wins."""
        require_internal(x_intentlock_internal_token)
        get_run_or_404(run_id)
        t = now()
        won = store.transition_run(run_id, ["created", "active"], status=body.status, completed_at=t,
                                   stop_reason=body.reason)
        if won is not None:
            store.append_event(run_id, event_type="run_completed", status="recorded",
                               message=body.reason or f"Run {body.status}", details={"status": body.status})
        run = store.get_run(run_id)
        return {"run": run.model_dump(mode="json"), "transitioned": won is not None}

    @app.post("/internal/events", status_code=201)
    def ingest_event(body: IngestEventBody,
                     x_intentlock_internal_token: str | None = Header(default=None)) -> dict:
        require_internal(x_intentlock_internal_token)
        get_run_or_404(body.run_id)
        ev = store.append_event(body.run_id, event_type=body.event_type, status="recorded",
                                request_id=body.request_id, message=body.message,
                                details=sanitize_details(body.details) if body.details else None)
        return ev.model_dump(mode="json")

    impact_api.register(app, store, artifacts, promoter, require_internal, lambda: now(), reviewers, reviewer_tokens)
    return app
