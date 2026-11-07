"""Authenticated, bounded control-plane transport using schema 0.1.0."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse, quote
import urllib.request
from jsonschema import Draft202012Validator, FormatChecker

class ControlPlaneError(Exception):
    pass

class Denied(Exception):
    def __init__(self, code, stop_run=False):
        super().__init__(code)
        self.stop_run = stop_run

SCHEMAS = Path(__file__).with_name('schemas')

def validate(kind, value):
    schema = json.loads((SCHEMAS / (kind + '.schema.json')).read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Client:
    def __init__(self, base_url, token):
        url = urlparse(base_url)
        if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1'))) or url.username or url.password or url.query or url.fragment or not token:
            raise ControlPlaneError('Invalid control-plane URL or missing internal token')
        self.base_url, self.token = base_url.rstrip('/'), token
        self.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('INTENTLOCK_API_URL', ''), os.environ.get('INTENTLOCK_INTERNAL_TOKEN', ''))

    def request(self, path, body=None):
        req = urllib.request.Request(self.base_url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'X-IntentLock-Internal-Token': self.token},
            method='GET' if body is None else 'POST')
        try:
            with self.opener.open(req, timeout=2) as response:
                data = response.read(1048577)
                if len(data) > 1048576:
                    raise ValueError('Response too large')
                return json.loads(data)
        except Exception:
            raise ControlPlaneError('CONTROL_PLANE_UNAVAILABLE') from None

    def authorize(self, envelope):
        try:
            validate('tool-request', envelope)
            decision = self.request('/internal/authorize', envelope)
            validate('authorization-decision', decision)
            if decision['request_id'] != envelope['request_id']:
                raise ValueError('Request ID mismatch')
            if decision['decision'] != 'allow' or decision.get('stop_run', False):
                raise Denied(decision['reason_code'], decision.get('stop_run', False) or decision['reason_code'] == 'RUN_NOT_ACTIVE')
            if decision['reason_code'] != 'ALLOW':
                raise ValueError('Inconsistent allow decision')
        except Denied:
            raise
        except Exception:
            raise Denied('POLICY_ENGINE_ERROR', True) from None

    def run(self, run_id):
        run = self.request('/api/runs/' + quote(run_id, safe=''))
        try:
            validate('run', run)
            if run['run_id'] != run_id:
                raise ValueError('Wrong run')
        except Exception:
            raise ControlPlaneError('INVALID_RUN_STATE') from None
        return run

    def register(self, run_id, pid):
        return self.request('/internal/runs/' + quote(run_id, safe='') + '/agent', {'agent_process_id': str(pid)})

    def event(self, run_id, message):
        return self.request('/internal/events', {'run_id': run_id, 'event_type': 'agent_event', 'message': message})
