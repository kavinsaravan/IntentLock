"""DynamoDB-backed store (single table).

Key design (table keys: pk S, sk S):
  CONTRACT#{id} / V#{version:08d}   data=JSON(contract) status=attr     immutable: Put with attribute_not_exists; only `status` is ever updated
  RUN#{id}      / META              run fields as native attrs + event_seq + tool_calls counters
  RUN#{id}      / EVT#{seq:012d}    data=JSON(event)                    seq allocated in a TransactWriteItems with the run's event_seq counter => gap-free
  REVIEW#{impact_report_id} / R#{reviewed_at}#{review_id}  data=JSON(review)

Atomicity:
  * run state transitions: UpdateItem with ConditionExpression on status (compare-and-set)
  * tool-call limit counting: UpdateItem ADD tool_calls 1 (returns previous value) - exact under concurrency
  * event ordering: counter bump + event Put in ONE transaction, so readers never see a gap
Only redacted data is ever written by callers (see intentlock_policy.redact); this module adds no content.
"""
import random
import threading
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Iterable

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from intentlock_policy.models import Event, ImpactReport, MissionContract, ReviewApproval, Run

from .store import ContractExists, ImpactExists, RunExists, StoreError, _iso, now

_ser = TypeSerializer()
_deser = TypeDeserializer()
_RUN_ATTRS = set(Run.model_fields)
_COND_FAIL = "ConditionalCheckFailedException"


def _typed(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _ser.serialize(v) for k, v in d.items()}


def _untyped(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _deser.deserialize(v) for k, v in d.items()}


class _Table:
    """Thin Table-like adapter over ONE shared, thread-safe low-level client.

    Replaces per-thread boto3 *resources* (each thread paid session + TLS setup on its first request, which
    showed up as ~1.5 s on the first authorization). Same call shapes and results as the Table resource.
    """

    def __init__(self, client: Any, name: str) -> None:
        self._c, self._n = client, name

    @staticmethod
    def _kw(kw: dict[str, Any]) -> dict[str, Any]:
        out = dict(kw)
        for k in ("Key", "Item", "ExclusiveStartKey", "ExpressionAttributeValues"):
            if k in out:
                out[k] = _typed(out[k])
        return out

    def put_item(self, **kw: Any) -> Any:
        return self._c.put_item(TableName=self._n, **self._kw(kw))

    def get_item(self, **kw: Any) -> Any:
        r = self._c.get_item(TableName=self._n, **self._kw(kw))
        if "Item" in r:
            r["Item"] = _untyped(r["Item"])
        return r

    def update_item(self, **kw: Any) -> Any:
        r = self._c.update_item(TableName=self._n, **self._kw(kw))
        if "Attributes" in r:
            r["Attributes"] = _untyped(r["Attributes"])
        return r

    def query(self, **kw: Any) -> Any:
        r = self._c.query(TableName=self._n, **self._kw(kw))
        r["Items"] = [_untyped(i) for i in r.get("Items", [])]
        if "LastEvaluatedKey" in r:
            r["LastEvaluatedKey"] = _untyped(r["LastEvaluatedKey"])
        return r


# DynamoDB rejects a plain write to an item while a transaction (our event append) is in flight on it.
_CONFLICT_CODES = frozenset({"TransactionConflictException", "ProvisionedThroughputExceededException", "ThrottlingException"})


def _retrying(fn: Any, attempts: int = 12) -> Any:
    """Retry a single-item write on transaction conflicts / throttling with short jittered backoff (bounded)."""
    for i in range(attempts):
        try:
            return fn()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in _CONFLICT_CODES and i < attempts - 1:
                time.sleep(random.uniform(0, 0.015 * (i + 1)))
                continue
            raise


# Bounded waits: a slow DynamoDB call fails (and authorization fails closed) instead of hanging a worker thread.
_CLIENT_CONFIG = Config(max_pool_connections=64, connect_timeout=1.5, read_timeout=2.0,
                        retries={"max_attempts": 2, "mode": "standard"})


class DynamoStore:
    def __init__(self, table_name: str, region: str | None = None, endpoint_url: str | None = None) -> None:
        self.table_name = table_name
        self._region, self._endpoint = region, endpoint_url
        self._client = boto3.client("dynamodb", region_name=region, endpoint_url=endpoint_url, config=_CLIENT_CONFIG)
        self._t = _Table(self._client, table_name)
        self._seq_hint: "OrderedDict[str, int]" = OrderedDict()   # run_id -> last event seq we wrote (saves a read per append)
        self._hint_lock = threading.Lock()
        self.backend = f"dynamodb:{table_name}@{self._client.meta.region_name}"

    def warm_up(self) -> None:
        """Open the connection and resolve credentials before the first real request (fire-and-forget at startup)."""
        try:
            self._client.get_item(TableName=self.table_name, Key=_typed({"pk": "WARMUP#", "sk": "x"}), ConsistentRead=False)
        except Exception:
            pass

    # ---------------------------------------------------------------- helpers
    def _wrap(self, e: ClientError) -> StoreError:
        return StoreError(e.response.get("Error", {}).get("Code", "DynamoDBError"))

    @staticmethod
    def _run_from_item(item: dict[str, Any]) -> Run:
        d = {k: (int(v) if isinstance(v, Decimal) else v) for k, v in item.items() if k in _RUN_ATTRS}
        return Run.model_validate(d)

    # -------------------------------------------------------------- contracts
    def put_contract(self, c: MissionContract) -> None:
        try:
            self._t.put_item(
                Item={"pk": f"CONTRACT#{c.contract_id}", "sk": f"V#{c.version:08d}",
                      "data": c.model_dump_json(), "status": c.status, "version": c.version},
                ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                raise ContractExists(f"{c.contract_id} v{c.version}") from e
            raise self._wrap(e) from e

    def get_contract(self, contract_id: str, version: int) -> MissionContract | None:
        try:
            r = self._t.get_item(Key={"pk": f"CONTRACT#{contract_id}", "sk": f"V#{version:08d}"}, ConsistentRead=True)
        except ClientError as e:
            raise self._wrap(e) from e
        it = r.get("Item")
        if not it:
            return None
        c = MissionContract.model_validate_json(it["data"])
        return c.model_copy(update={"status": it["status"]})  # status attr is authoritative

    def latest_version(self, contract_id: str) -> int:
        try:
            r = self._t.query(KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
                              ExpressionAttributeValues={":p": f"CONTRACT#{contract_id}", ":s": "V#"},
                              ScanIndexForward=False, Limit=1, ConsistentRead=True)
        except ClientError as e:
            raise self._wrap(e) from e
        items = r.get("Items", [])
        return int(items[0]["version"]) if items else 0

    def set_contract_status(self, contract_id: str, version: int, status: str) -> None:
        try:
            self._t.update_item(Key={"pk": f"CONTRACT#{contract_id}", "sk": f"V#{version:08d}"},
                                UpdateExpression="SET #s = :s", ConditionExpression="attribute_exists(pk)",
                                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": status})
        except ClientError as e:
            raise self._wrap(e) from e

    # ------------------------------------------------------------------- runs
    def put_run(self, r: Run) -> None:
        item = {k: _iso(v) for k, v in r.model_dump().items() if v is not None}
        item.update(pk=f"RUN#{r.run_id}", sk="META", event_seq=0, tool_calls=0)
        try:
            self._t.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                raise RunExists(r.run_id) from e
            raise self._wrap(e) from e

    def get_run(self, run_id: str) -> Run | None:
        try:
            r = self._t.get_item(Key={"pk": f"RUN#{run_id}", "sk": "META"}, ConsistentRead=True)
        except ClientError as e:
            raise self._wrap(e) from e
        return self._run_from_item(r["Item"]) if r.get("Item") else None

    def _update_run(self, run_id: str, fields: dict[str, Any], condition: str,
                    extra_names: dict[str, str] | None = None, extra_values: dict[str, Any] | None = None) -> Run | None:
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": _iso(v) for k, v in fields.items()}
        names.update(extra_names or {}); values.update(extra_values or {})
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        try:
            r = _retrying(lambda: self._t.update_item(
                Key={"pk": f"RUN#{run_id}", "sk": "META"}, UpdateExpression=expr, ConditionExpression=condition,
                ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW"))
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                return None
            raise self._wrap(e) from e
        return self._run_from_item(r["Attributes"])

    def update_run(self, run_id: str, **fields: Any) -> Run:
        if "status" in fields:
            raise ValueError("use transition_run for status changes")
        fields = {k: v for k, v in fields.items() if k in _RUN_ATTRS and v is not None}
        if not fields:
            r = self.get_run(run_id)
            if r is None:
                raise StoreError("run not found")
            return r
        r = self._update_run(run_id, fields, "attribute_exists(pk)")
        if r is None:
            raise StoreError("run not found")
        return r

    def transition_run(self, run_id: str, from_statuses: Iterable[str], **fields: Any) -> Run | None:
        froms = list(from_statuses)
        fields = {k: v for k, v in fields.items() if k in _RUN_ATTRS and v is not None}
        names = {"#status": "status"}
        values = {f":f{i}": s for i, s in enumerate(froms)}
        cond = "#status IN (" + ", ".join(values) + ")"
        return self._update_run(run_id, fields, cond, names, values)

    def reserve_tool_call(self, run_id: str) -> int:
        try:
            r = _retrying(lambda: self._t.update_item(
                Key={"pk": f"RUN#{run_id}", "sk": "META"}, UpdateExpression="ADD tool_calls :one",
                ConditionExpression="attribute_exists(pk)", ExpressionAttributeValues={":one": 1}, ReturnValues="UPDATED_NEW"))
        except ClientError as e:
            raise self._wrap(e) from e
        return int(r["Attributes"]["tool_calls"]) - 1

    # ----------------------------------------------------------------- events
    def append_event(self, run_id: str, **fields: Any) -> Event:
        key = {"pk": f"RUN#{run_id}", "sk": "META"}
        for attempt in range(60):
            with self._hint_lock:
                prev = self._seq_hint.get(run_id)
            if prev is None:          # no (or stale) hint: read the authoritative counter
                cur = self._t.get_item(Key=key, ConsistentRead=True).get("Item")
                if cur is None:
                    raise StoreError("run not found")
                prev = int(cur["event_seq"])
            seq = prev + 1
            ev = Event(event_id=f"evt_{run_id}_{seq:08d}", run_id=run_id, seq=seq, timestamp=now(), **fields)
            item = {"pk": f"RUN#{run_id}", "sk": f"EVT#{seq:012d}", "data": ev.model_dump_json(), "seq": seq}
            try:
                # The condition `event_seq = :p` keeps this exact even when the hint is stale or another writer raced us.
                self._client.transact_write_items(TransactItems=[
                    {"Update": {"TableName": self.table_name, "Key": _typed(key),
                                "UpdateExpression": "SET event_seq = :n",
                                "ConditionExpression": "event_seq = :p",
                                "ExpressionAttributeValues": _typed({":n": seq, ":p": prev})}},
                    {"Put": {"TableName": self.table_name, "Item": _typed(item),
                             "ConditionExpression": "attribute_not_exists(pk)"}},
                ])
                with self._hint_lock:
                    self._seq_hint[run_id] = seq
                    self._seq_hint.move_to_end(run_id)
                    while len(self._seq_hint) > 512:
                        self._seq_hint.popitem(last=False)
                return ev
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("TransactionCanceledException", "TransactionConflictException",
                            "ProvisionedThroughputExceededException"):
                    with self._hint_lock:
                        self._seq_hint.pop(run_id, None)     # force an authoritative read on the retry
                    time.sleep(random.uniform(0, 0.01 * (attempt + 1)))  # contention: retry
                    continue
                raise self._wrap(e) from e
            except RuntimeError as e:
                # Moto's in-memory DynamoDB is not fully thread-safe: concurrent transact_write_items
                # can trigger "dictionary changed size during iteration" inside moto internals.
                # Real DynamoDB never raises RuntimeError. Retry as with TransactionConflictException.
                if "dictionary changed size during iteration" not in str(e):
                    raise
                with self._hint_lock:
                    self._seq_hint.pop(run_id, None)
                time.sleep(random.uniform(0, 0.01 * (attempt + 1)))
                continue
        raise StoreError("event append contention: gave up")

    def update_event(self, run_id: str, seq: int, base: Event | None = None, **fields: Any) -> Event:
        """Update an event. If the caller already holds the event (`base`), skip the read round trip."""
        key = {"pk": f"RUN#{run_id}", "sk": f"EVT#{seq:012d}"}
        try:
            if base is not None and base.run_id == run_id and base.seq == seq:
                cur = base
            else:
                it = self._t.get_item(Key=key, ConsistentRead=True).get("Item")
                if not it:
                    raise StoreError("event not found")
                cur = Event.model_validate_json(it["data"])
            ev = cur.model_copy(update=fields)
            self._t.update_item(Key=key, UpdateExpression="SET #d = :d", ConditionExpression="attribute_exists(pk)",
                                ExpressionAttributeNames={"#d": "data"}, ExpressionAttributeValues={":d": ev.model_dump_json()})
        except ClientError as e:
            raise self._wrap(e) from e
        return ev

    def list_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        out: list[Event] = []
        kwargs: dict[str, Any] = dict(
            KeyConditionExpression="pk = :p AND sk BETWEEN :lo AND :hi",
            ExpressionAttributeValues={":p": f"RUN#{run_id}", ":lo": f"EVT#{after_seq + 1:012d}", ":hi": "EVT#999999999999"},
            ConsistentRead=True)
        try:
            while True:
                r = self._t.query(**kwargs)
                out += [Event.model_validate_json(i["data"]) for i in r["Items"]]
                if "LastEvaluatedKey" not in r:
                    return out
                kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        except ClientError as e:
            raise self._wrap(e) from e

    # ---------------------------------------------------------------- reviews
    def put_review(self, r: ReviewApproval) -> None:
        try:
            self._t.put_item(Item={"pk": f"REVIEW#{r.impact_report_id}",
                                   "sk": f"R#{_iso(r.reviewed_at)}#{r.review_id}", "data": r.model_dump_json()},
                             ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                raise ContractExists(f"review {r.review_id} exists") from e
            raise self._wrap(e) from e

    def list_reviews(self, impact_report_id: str) -> list[ReviewApproval]:
        try:
            r = self._t.query(KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
                              ExpressionAttributeValues={":p": f"REVIEW#{impact_report_id}", ":s": "R#"}, ConsistentRead=True)
        except ClientError as e:
            raise self._wrap(e) from e
        return [ReviewApproval.model_validate_json(i["data"]) for i in r["Items"]]

    # ------------------------------------------- impact / final review / promotion
    def put_impact(self, r: ImpactReport) -> None:
        try:
            self._t.put_item(Item={"pk": f"IMPACT#{r.impact_report_id}", "sk": "REPORT", "data": r.model_dump_json(),
                                   "run_id": r.run_id}, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                raise ImpactExists(r.impact_report_id) from e
            raise self._wrap(e) from e

    def get_impact(self, impact_report_id: str) -> ImpactReport | None:
        try:
            it = self._t.get_item(Key={"pk": f"IMPACT#{impact_report_id}", "sk": "REPORT"}, ConsistentRead=True).get("Item")
        except ClientError as e:
            raise self._wrap(e) from e
        return ImpactReport.model_validate_json(it["data"]) if it else None

    def get_final_review(self, impact_report_id: str) -> ReviewApproval | None:
        try:
            it = self._t.get_item(Key={"pk": f"REVIEW#{impact_report_id}", "sk": "FINAL"}, ConsistentRead=True).get("Item")
        except ClientError as e:
            raise self._wrap(e) from e
        return ReviewApproval.model_validate_json(it["data"]) if it else None

    def put_final_review(self, r: ReviewApproval) -> tuple[ReviewApproval, bool]:
        try:
            self._t.put_item(Item={"pk": f"REVIEW#{r.impact_report_id}", "sk": "FINAL", "data": r.model_dump_json()},
                             ConditionExpression="attribute_not_exists(pk)")
            return r, True
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                cur = self.get_final_review(r.impact_report_id)
                if cur is not None:
                    return cur, False
            raise self._wrap(e) from e

    def put_promotion(self, rec: dict[str, Any]) -> bool:
        item = {k: v for k, v in rec.items() if v is not None}
        item.update(pk=f"PROMO#{rec['impact_report_id']}", sk="STATE")
        try:
            self._t.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                return False
            raise self._wrap(e) from e

    @staticmethod
    def _promo_from_item(it: dict[str, Any]) -> dict[str, Any]:
        return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in it.items() if k not in ("pk", "sk")}

    def get_promotion(self, impact_report_id: str) -> dict[str, Any] | None:
        try:
            it = self._t.get_item(Key={"pk": f"PROMO#{impact_report_id}", "sk": "STATE"}, ConsistentRead=True).get("Item")
        except ClientError as e:
            raise self._wrap(e) from e
        return self._promo_from_item(it) if it else None

    def transition_promotion(self, impact_report_id: str, from_statuses: Iterable[str], **fields: Any) -> dict[str, Any] | None:
        froms = list(from_statuses)
        fields = {k: v for k, v in fields.items() if v is not None}
        names = {f"#{k}": k for k in fields}; names["#status"] = "status"
        values = {f":{k}": v for k, v in fields.items()}; values.update({f":f{i}": s for i, s in enumerate(froms)})
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        try:
            r = self._t.update_item(Key={"pk": f"PROMO#{impact_report_id}", "sk": "STATE"}, UpdateExpression=expr,
                                    ConditionExpression="#status IN (" + ", ".join(f":f{i}" for i in range(len(froms))) + ")",
                                    ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
        except ClientError as e:
            if e.response["Error"]["Code"] == _COND_FAIL:
                return None
            raise self._wrap(e) from e
        return self._promo_from_item(r["Attributes"])


TABLE_DEFINITION: dict[str, Any] = {
    "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
    "BillingMode": "PAY_PER_REQUEST",
}
