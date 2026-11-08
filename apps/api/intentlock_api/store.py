"""Store interface, errors, config-driven factory, and the LOCAL FALLBACK store.

Two implementations share one contract (tested identically in tests/test_store_contract.py):
  * LocalMemoryStore  - in-memory, single process, NOT persisted. Local development only.
  * DynamoStore       - dynamo_store.py, real DynamoDB persistence.

Selection (env): INTENTLOCK_STORE = "memory" (default) | "dynamodb".
DynamoDB: INTENTLOCK_TABLE (required), AWS_REGION, INTENTLOCK_DDB_ENDPOINT (optional, local emulator).
"""
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from intentlock_policy.models import Event, ImpactReport, MissionContract, ReviewApproval, Run


def now() -> datetime:
    return datetime.now(timezone.utc)


class ContractExists(Exception):
    """A contract version already exists; versions are immutable."""


class RunExists(Exception):
    pass


class ImpactExists(Exception):
    """An impact report id already exists; reports are immutable."""


class StoreError(Exception):
    """Persistence failure. Callers must fail closed."""


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


class LocalMemoryStore:
    """LOCAL FALLBACK: in-memory, single-process, NOT DynamoDB, nothing is persisted."""

    backend = "local-memory (NOT DynamoDB)"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._contracts: dict[tuple[str, int], MissionContract] = {}
        self._runs: dict[str, Run] = {}
        self._event_seq: dict[str, int] = {}
        self._tool_calls: dict[str, int] = {}
        self._events: dict[str, list[Event]] = {}
        self._reviews: dict[str, list[ReviewApproval]] = {}
        self._impacts: dict[str, ImpactReport] = {}
        self._final_reviews: dict[str, ReviewApproval] = {}
        self._promotions: dict[str, dict[str, Any]] = {}

    # ---- contracts (content immutable; only `status` may change) ----
    def put_contract(self, c: MissionContract) -> None:
        with self._lock:
            if (c.contract_id, c.version) in self._contracts:
                raise ContractExists(f"{c.contract_id} v{c.version}")
            self._contracts[(c.contract_id, c.version)] = c

    def get_contract(self, contract_id: str, version: int) -> MissionContract | None:
        with self._lock:
            return self._contracts.get((contract_id, version))

    def latest_version(self, contract_id: str) -> int:
        with self._lock:
            return max((v for (cid, v) in self._contracts if cid == contract_id), default=0)

    def set_contract_status(self, contract_id: str, version: int, status: str) -> None:
        with self._lock:
            c = self._contracts[(contract_id, version)]
            self._contracts[(contract_id, version)] = c.model_copy(update={"status": status})

    # ---- runs ----
    def put_run(self, r: Run) -> None:
        with self._lock:
            if r.run_id in self._runs:
                raise RunExists(r.run_id)
            self._runs[r.run_id] = r
            self._event_seq[r.run_id] = 0
            self._tool_calls[r.run_id] = 0

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def update_run(self, run_id: str, **fields: Any) -> Run:
        """Set non-status fields (agent/sandbox ids). Status changes go through transition_run."""
        if "status" in fields:
            raise ValueError("use transition_run for status changes")
        with self._lock:
            r = self._runs[run_id].model_copy(update=fields)
            self._runs[run_id] = r
            return r

    def transition_run(self, run_id: str, from_statuses: Iterable[str], **fields: Any) -> Run | None:
        """Atomic compare-and-set on status. Returns the updated run, or None if the
        current status is not in from_statuses (another actor already moved it)."""
        with self._lock:
            r = self._runs.get(run_id)
            if r is None or r.status not in set(from_statuses):
                return None
            r = r.model_copy(update=fields)
            self._runs[run_id] = r
            return r

    def reserve_tool_call(self, run_id: str) -> int:
        """Atomically count one request for this run; returns the number of PRIOR requests."""
        with self._lock:
            prior = self._tool_calls[run_id]
            self._tool_calls[run_id] = prior + 1
            return prior

    # ---- events (seq is gap-free and monotonic per run; event_id is deterministic) ----
    def append_event(self, run_id: str, **fields: Any) -> Event:
        with self._lock:
            seq = self._event_seq[run_id] + 1
            self._event_seq[run_id] = seq
            ev = Event(event_id=f"evt_{run_id}_{seq:08d}", run_id=run_id, seq=seq,
                       timestamp=now(), **fields)
            self._events.setdefault(run_id, []).append(ev)
            return ev

    def update_event(self, run_id: str, seq: int, base: Event | None = None, **fields: Any) -> Event:
        with self._lock:
            lst = self._events[run_id]
            ev = lst[seq - 1].model_copy(update=fields)
            lst[seq - 1] = ev
            return ev

    def list_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._events.get(run_id, []) if e.seq > after_seq]

    # ---- reviews ----
    def put_review(self, r: ReviewApproval) -> None:
        with self._lock:
            lst = self._reviews.setdefault(r.impact_report_id, [])
            if any(x.review_id == r.review_id for x in lst):
                raise ContractExists(f"review {r.review_id} exists")
            lst.append(r)

    def list_reviews(self, impact_report_id: str) -> list[ReviewApproval]:
        with self._lock:
            return sorted(self._reviews.get(impact_report_id, []), key=lambda r: (r.reviewed_at, r.review_id))

    # ---- impact reports (immutable), ONE final review per report, promotion records ----
    def put_impact(self, r: ImpactReport) -> None:
        with self._lock:
            if r.impact_report_id in self._impacts:
                raise ImpactExists(r.impact_report_id)
            self._impacts[r.impact_report_id] = r

    def get_impact(self, impact_report_id: str) -> ImpactReport | None:
        with self._lock:
            return self._impacts.get(impact_report_id)

    def put_final_review(self, r: ReviewApproval) -> tuple[ReviewApproval, bool]:
        """Atomic first-decision-wins. Returns (stored review, created?)."""
        with self._lock:
            cur = self._final_reviews.get(r.impact_report_id)
            if cur is not None:
                return cur, False
            self._final_reviews[r.impact_report_id] = r
            self.put_review(r)
            return r, True

    def get_final_review(self, impact_report_id: str) -> ReviewApproval | None:
        with self._lock:
            return self._final_reviews.get(impact_report_id)

    def put_promotion(self, rec: dict[str, Any]) -> bool:
        with self._lock:
            if rec["impact_report_id"] in self._promotions:
                return False
            self._promotions[rec["impact_report_id"]] = dict(rec)
            return True

    def get_promotion(self, impact_report_id: str) -> dict[str, Any] | None:
        with self._lock:
            p = self._promotions.get(impact_report_id)
            return dict(p) if p else None

    def transition_promotion(self, impact_report_id: str, from_statuses: Iterable[str], **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            p = self._promotions.get(impact_report_id)
            if p is None or p["status"] not in set(from_statuses):
                return None
            p.update(fields)
            return dict(p)


DEFAULT_REGION = "us-east-1"   # the team's confirmed AWS region


def aws_region(env=None) -> str:
    """One region for every AWS client in the control plane: explicit env wins, otherwise us-east-1.
    (Never the ambient CLI profile's region, which may differ: the default profile here is us-west-1.)"""
    env = os.environ if env is None else env
    return env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def create_store_from_env(env: dict[str, str] | None = None):
    env = os.environ if env is None else env
    kind = (env.get("INTENTLOCK_STORE") or "memory").lower()
    if kind == "memory":
        return LocalMemoryStore()
    if kind == "dynamodb":
        from .dynamo_store import DynamoStore

        table = env.get("INTENTLOCK_TABLE")
        if not table:
            raise StoreError("INTENTLOCK_TABLE is required when INTENTLOCK_STORE=dynamodb")
        return DynamoStore(table, region=aws_region(env),
                           endpoint_url=env.get("INTENTLOCK_DDB_ENDPOINT") or None)
    raise StoreError(f"unknown INTENTLOCK_STORE {kind!r} (expected 'memory' or 'dynamodb')")
