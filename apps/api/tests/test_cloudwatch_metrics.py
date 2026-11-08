"""CloudWatch EMF never carries request arguments, tokens, or patch data."""
import json
from types import SimpleNamespace

from intentlock_api.lambda_handler import _emit_metrics


def test_authorizer_emits_only_fixed_metric_fields(monkeypatch, capsys):
    monkeypatch.setenv('AWS_LAMBDA_FUNCTION_NAME', 'intentlock-authorizer')
    _emit_metrics(SimpleNamespace(decision='deny', latency_ms=7.5))
    record = json.loads(capsys.readouterr().out)
    assert record['ToolRequestsDenied'] == 1
    assert record['ToolRequestsAllowed'] == 0
    assert record['AuthorizationLatencyMs'] == 7.5
    assert record['Component'] == 'Authorizer'
    assert record['_aws']['CloudWatchMetrics'][0]['Namespace'] == 'IntentLock'
    assert set(record) == {'_aws', 'Component', 'AuthorizationLatencyMs', 'ToolRequestsAllowed', 'ToolRequestsDenied'}
