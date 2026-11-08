"""Keep the Converse request compatible with the selected Bedrock Haiku profile."""
from unittest.mock import Mock, patch

from intentlock_api.bedrock import bedrock_invoker, propose_draft


def test_invoker_omits_deprecated_temperature(monkeypatch):
    monkeypatch.setenv('INTENTLOCK_BEDROCK_MODEL_ID', 'us.anthropic.claude-haiku-5-5')
    client = Mock()
    client.converse.return_value = {'output': {'message': {'content': [
        {'reasoningContent': {'reasoningText': {'text': 'hidden'}}}, {'text': '{}'}]}}}
    with patch('boto3.client', return_value=client):
        assert bedrock_invoker()('system', 'user') == '{}'
    config = client.converse.call_args.kwargs['inferenceConfig']
    assert config == {'maxTokens': 1500}


def test_proposal_retries_wrong_test_catalog():
    import json
    from pathlib import Path
    fixture = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts/fixtures/contract-draft.json').read_text())
    wrong = dict(fixture, approved_tests={'auth-tests': {'argv':['pytest','tests/auth'], 'timeout_seconds':60}})
    calls = iter([json.dumps(wrong), json.dumps(fixture)])
    draft, attempts = propose_draft('fix auth', 'intentlock-demo', fixture['base_commit'], lambda _system, _user: next(calls))
    assert attempts == 2
    assert draft.approved_tests['auth-tests'].argv == ['npm','run','test:auth']
