"""Measure REAL authorization latency against the deployed DynamoDB table, and count DynamoDB calls per request.

    AWS_PROFILE=intentlock AWS_REGION=us-east-1 .venv/bin/python infrastructure/control-plane/measure_authorize_latency.py [N]

What it does (use it on the DEMO MACHINE and network before going live):
  * seeds ONE contract + ONE run with unique ids in the real table,
  * times N sequential `service.authorize_request` calls (the exact path /internal/authorize uses),
  * probes the raw round-trip time of one DynamoDB GetItem,
  * deletes every item it created. Nothing is billed beyond a few hundred requests (cents or less).
It refuses to run unless the caller is account 247495661854 under profile `intentlock`. No Bedrock, no IAM, no new resources.
"""
import collections
import json
import os
import pathlib
import statistics
import sys
import threading
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "packages/policy-engine"), str(ROOT / "apps/api")]

if os.environ.get("AWS_PROFILE") != "intentlock":
    sys.exit("refusing to run: set AWS_PROFILE=intentlock (the default profile must never be used for IntentLock)")

import boto3  # noqa: E402
import botocore.handlers  # noqa: E402

# Count every DynamoDB API call made by ANY boto3 client/session created after this point.
CALLS: collections.Counter = collections.Counter()
_lock = threading.Lock()


def _count(model, **_kwargs):
    with _lock:
        CALLS[model.name] += 1


botocore.handlers.BUILTIN_HANDLERS.append(("before-call.dynamodb", _count))

from intentlock_api import service  # noqa: E402
from intentlock_api.dynamo_store import DynamoStore  # noqa: E402
from intentlock_api.store import now  # noqa: E402
from intentlock_policy.models import MissionContract, Run  # noqa: E402

TABLE, REGION, EXPECTED_ACCOUNT = "intentlock-control-plane", "us-east-1", "247495661854"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40

sess = boto3.Session(profile_name="intentlock", region_name=REGION)
if sess.client("sts").get_caller_identity()["Account"] != EXPECTED_ACCOUNT:
    sys.exit("wrong AWS account; aborting")

store = DynamoStore(TABLE, region=REGION)
tag = uuid.uuid4().hex[:8]
fixture = json.loads((ROOT / "packages/contracts/fixtures/contract.json").read_text())
fixture.update(contract_id=f"c_latency_{tag}", version=1)
fixture["limits"]["max_tool_calls"] = 10000
contract = MissionContract.model_validate(fixture)
store.put_contract(contract)
t = now()
run = Run(run_id=f"run_latency_{tag}", contract_id=contract.contract_id, contract_version=1, status="active",
          base_commit=contract.base_commit, created_at=t, started_at=t)
store.put_run(run)


def request():
    return {"request_id": f"q_{uuid.uuid4().hex[:8]}", "run_id": run.run_id, "contract_id": contract.contract_id,
            "contract_version": 1, "tool": "read_file", "arguments": {"path": "src/auth/session.ts"},
            "agent_id": "latency-check", "timestamp": "2026-11-07T10:00:00Z"}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(p / 100 * len(xs) + 0.4999)) - 1))]


try:
    # Raw round trip: one GetItem for a key that does not exist (strongly consistent, like our reads).
    cli = sess.client("dynamodb")
    cli.get_item(TableName=TABLE, Key={"pk": {"S": "RTT#warmup"}, "sk": {"S": "x"}}, ConsistentRead=True)
    rtt = []
    for _ in range(20):
        t0 = time.perf_counter()
        cli.get_item(TableName=TABLE, Key={"pk": {"S": "RTT#probe"}, "sk": {"S": "x"}}, ConsistentRead=True)
        rtt.append((time.perf_counter() - t0) * 1000)

    lat, per_req_calls = [], []
    for i in range(N):
        with _lock:
            before = sum(CALLS.values())
        t0 = time.perf_counter()
        d = service.authorize_request(request(), store)
        lat.append((time.perf_counter() - t0) * 1000)
        with _lock:
            per_req_calls.append(sum(CALLS.values()) - before)
        assert d.decision == "allow", d
    steady = lat[1:]   # the first call may include one-off TLS/session setup
    print(f"region {REGION} | table {TABLE} | N={N} sequential authorizations")
    print(f"raw DynamoDB GetItem round trip : p50 {statistics.median(rtt):6.1f} | p95 {pct(rtt, 95):6.1f} | max {max(rtt):6.1f} ms")
    print(f"DynamoDB calls per authorization: {statistics.median(per_req_calls):.0f} (min {min(per_req_calls)}, max {max(per_req_calls)})")
    print(f"first authorization (setup)     : {lat[0]:6.1f} ms")
    print(f"authorization latency (steady)  : p50 {statistics.median(steady):6.1f} | p95 {pct(steady, 95):6.1f} | max {max(steady):6.1f} ms")
    print("Person 2's MCP client HTTP timeout: 2000 ms  ->  headroom at max = %.1fx" % (2000 / max(steady)))
    print("operations (total):", dict(CALLS))
finally:
    # Remove everything this run created (contract, run META, its events).
    tbl = sess.resource("dynamodb").Table(TABLE)
    keys = [("CONTRACT#" + contract.contract_id, "V#00000001"), ("RUN#" + run.run_id, "META")]
    q = tbl.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": "RUN#" + run.run_id}, ConsistentRead=True)
    keys += [(i["pk"], i["sk"]) for i in q["Items"]]
    while "LastEvaluatedKey" in q:
        q = tbl.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": "RUN#" + run.run_id},
                      ConsistentRead=True, ExclusiveStartKey=q["LastEvaluatedKey"])
        keys += [(i["pk"], i["sk"]) for i in q["Items"]]
    with tbl.batch_writer() as bw:
        for pk, sk in set(keys):
            bw.delete_item(Key={"pk": pk, "sk": sk})
    left = tbl.scan(Select="COUNT", ConsistentRead=True)["Count"]
    print(f"cleanup: deleted {len(set(keys))} items it created; items left in table: {left}")
