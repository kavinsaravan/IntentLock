"""AWS Lambda authorization handler (deterministic policy; NEVER calls Bedrock).

Handler: intentlock_api.lambda_handler.handler

Accepted events
  1. Direct invoke: the event IS a ToolRequest object  -> returns an AuthorizationDecision object.
  2. Lambda Function URL / API Gateway proxy event: JSON ToolRequest in `body`
     -> returns {"statusCode": 200, "body": "<AuthorizationDecision JSON>"}.
Anything else, any error, any store/config failure -> a DENY decision (fail closed).
Caller authentication is IAM (direct invoke or a Function URL with AuthType AWS_IAM); there is no
shared token here. The AgentCore Gateway interceptor payload mapping is NOT implemented/verified.

State and decisions live in DynamoDB (INTENTLOCK_STORE=dynamodb, INTENTLOCK_TABLE). A misconfigured
function (memory store without INTENTLOCK_ALLOW_MEMORY_STORE=1) denies everything.
"""
import json
import logging
import os
import time
from typing import Any

from .service import authorize_request, error_decision
from .store import StoreError, create_store_from_env

# The Lambda Python runtime leaves the root logger at WARNING, which would silently drop the INFO audit lines
# (intentlock.audit) from CloudWatch. Enable INFO for our own loggers only.
logging.getLogger("intentlock").setLevel(logging.INFO)
log = logging.getLogger("intentlock.lambda")
_store = None


def _emit_metrics(decision) -> None:
    """CloudWatch EMF through Lambda stdout, with fixed low-cardinality dimensions."""
    if not os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        return
    allowed = decision.decision == 'allow'
    record = {
        '_aws': {'Timestamp': int(time.time() * 1000), 'CloudWatchMetrics': [{
            'Namespace': 'IntentLock', 'Dimensions': [['Component']],
            'Metrics': [{'Name': 'AuthorizationLatencyMs', 'Unit': 'Milliseconds'},
                        {'Name': 'ToolRequestsAllowed', 'Unit': 'Count'},
                        {'Name': 'ToolRequestsDenied', 'Unit': 'Count'}]}]},
        'Component': 'Authorizer', 'AuthorizationLatencyMs': decision.latency_ms,
        'ToolRequestsAllowed': 1 if allowed else 0,
        'ToolRequestsDenied': 0 if allowed else 1,
    }
    print(json.dumps(record, separators=(',', ':')), flush=True)


def get_store():
    global _store
    if _store is None:
        if os.environ.get("INTENTLOCK_STORE", "").lower() != "dynamodb" and os.environ.get("INTENTLOCK_ALLOW_MEMORY_STORE") != "1":
            raise StoreError("Lambda requires INTENTLOCK_STORE=dynamodb")
        _store = create_store_from_env()
    return _store


def _extract(event: Any) -> tuple[Any, bool]:
    """Return (raw ToolRequest candidate, is_proxy_event)."""
    if isinstance(event, dict) and "body" in event and ("requestContext" in event or "headers" in event or "httpMethod" in event):
        body = event.get("body")
        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body or "").decode("utf-8", "replace")
        try:
            return json.loads(body) if isinstance(body, str) else body, True
        except ValueError:
            return None, True
    return event, False


def handler(event: Any, context: Any = None) -> Any:
    raw, proxy = _extract(event)
    rid = raw.get("request_id") if isinstance(raw, dict) and isinstance(raw.get("request_id"), str) else "unknown"
    try:
        decision = authorize_request(raw, get_store())
    except Exception as e:  # store down, bad config, bug: fail closed, log only the error class
        log.error("authorization failed closed: %s", type(e).__name__)
        decision = error_decision(rid)
    out = decision.model_dump(mode="json")
    _emit_metrics(decision)
    if proxy:
        return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps(out)}
    return out
