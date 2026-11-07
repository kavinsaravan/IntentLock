"""LOCAL FALLBACK store: in-memory, single-process, NOT DynamoDB.

Same interface the DynamoDB store will implement (Milestone 3). Nothing here
is persisted. Do not describe this as an AWS integration.
"""
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from intentlock_policy.models import Event, MissionContract, Run


def now() -> datetime:
    return datetime.now(timezone.utc)


class LocalMemoryStore:
    backend = "local-memory (NOT DynamoDB)"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._contracts: dict[tuple[str, int], MissionContract] = {}
        self._runs: dict[str, Run] = {}
        self._events: dict[str, list[Event]] = {}

    # contracts
    def put_contract(self, c: MissionContract) -> None:
        with self.lock:
            self._contracts[(c.contract_id, c.version)] = c

    def get_contract(self, contract_id: str, version: int) -> MissionContract | None:
        return self._contracts.get((contract_id, version))

    def latest_version(self, contract_id: str) -> int:
        with self.lock:
            return max((v for (cid, v) in self._contracts if cid == contract_id), default=0)

    def set_contract_status(self, contract_id: str, version: int, status: str) -> None:
        with self.lock:
            c = self._contracts[(contract_id, version)]
            self._contracts[(contract_id, version)] = c.model_copy(update={"status": status})

    # runs
    def put_run(self, r: Run) -> None:
        with self.lock:
            self._runs[r.run_id] = r

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def update_run(self, run_id: str, **fields: Any) -> Run:
        with self.lock:
            r = self._runs[run_id].model_copy(update=fields)
            self._runs[run_id] = r
            return r

    # events
    def append_event(self, run_id: str, **fields: Any) -> Event:
        with self.lock:
            lst = self._events.setdefault(run_id, [])
            ev = Event(event_id="evt_" + uuid.uuid4().hex[:16], run_id=run_id,
                       seq=len(lst) + 1, timestamp=now(), **fields)
            lst.append(ev)
            return ev

    def update_event(self, run_id: str, seq: int, **fields: Any) -> Event:
        with self.lock:
            lst = self._events[run_id]
            ev = lst[seq - 1].model_copy(update=fields)
            lst[seq - 1] = ev
            return ev

    def list_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        with self.lock:
            return [e for e in self._events.get(run_id, []) if e.seq > after_seq]

    def count_tool_requests(self, run_id: str) -> int:
        with self.lock:
            return sum(1 for e in self._events.get(run_id, []) if e.event_type == "tool_request")
