"""Local promoter endpoint with independent review, run, artifact and base checks."""
import argparse
from datetime import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import urllib.request


def _module(path):
    spec = importlib.util.spec_from_file_location('intentlock_promote', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _module(Path(__file__).with_name('promote.py'))
artifacts_module = _module(Path(__file__).resolve().parents[1] / 'sandbox-provider' / 'artifacts.py')


class Refused(Exception):
    pass


class Verifier:
    def __init__(self, repository, artifact_dir, api_url, internal_token):
        self.repository = Path(repository).resolve(strict=True)
        self.artifacts = artifact_dir if hasattr(artifact_dir, 'read') else artifacts_module.LocalArtifacts(artifact_dir)
        self.api_url = api_url.rstrip('/')
        self.internal_token = internal_token
        if not (self.api_url.startswith('http://127.0.0.1:') or self.api_url.startswith('http://localhost:')):
            raise ValueError('promoter control plane must be local')

    def get(self, path):
        req = urllib.request.Request(self.api_url + path, headers={'X-IntentLock-Internal-Token': self.internal_token})
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    def verify(self, request):
        required = {'impact_report_id', 'run_id', 'contract_id', 'contract_version', 'base_commit', 'patch_sha256', 'patch_s3_key', 'review'}
        if not isinstance(request, dict) or set(request) != required:
            raise Refused('INVALID_REQUEST')
        report_id = request['impact_report_id']
        run_id = request['run_id']
        if not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', x) for x in (report_id, run_id, request['contract_id'])):
            raise Refused('INVALID_REQUEST')
        if not isinstance(request['contract_version'], int) or request['contract_version'] < 1:
            raise Refused('INVALID_REQUEST')
        if not isinstance(request['patch_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', request['patch_sha256']):
            raise Refused('INVALID_REQUEST')
        if not isinstance(request['base_commit'], str) or not re.fullmatch('[0-9a-f]{40}', request['base_commit']):
            raise Refused('INVALID_REQUEST')
        key = f"runs/{run_id}/patches/{request['patch_sha256']}.patch"
        if request['patch_s3_key'] != key:
            raise Refused('PATCH_KEY_MISMATCH')
        report = self.get('/api/impact/' + report_id)
        for field in ('impact_report_id', 'run_id', 'contract_id', 'contract_version', 'base_commit', 'patch_sha256', 'patch_s3_key'):
            if report.get(field) != request[field]:
                raise Refused('IMPACT_MISMATCH')
        if report.get('decision') not in ('pass', 'review_required') or any(x.get('severity') in ('critical', 'high') for x in report.get('findings', [])):
            raise Refused('IMPACT_BLOCKED')
        status = self.get('/api/impact/' + report_id + '/status')
        review = status.get('review')
        if not isinstance(review, dict) or review.get('decision') != 'approve':
            raise Refused('NOT_APPROVED')
        if not isinstance(request['review'], dict):
            raise Refused('REVIEW_MISMATCH')
        for field in ('review_id', 'reviewer', 'decision'):
            if review.get(field) != request['review'].get(field):
                raise Refused('REVIEW_MISMATCH')
        try:
            if datetime.fromisoformat(review['reviewed_at']) != datetime.fromisoformat(request['review']['reviewed_at']):
                raise Refused('REVIEW_MISMATCH')
        except (ValueError, TypeError, KeyError):
            raise Refused('REVIEW_MISMATCH') from None
        if review.get('patch_sha256') != request['patch_sha256'] or review.get('base_commit') != request['base_commit']:
            raise Refused('REVIEW_MISMATCH')
        promotion = status.get('promotion')
        if not isinstance(promotion, dict) or promotion.get('status') != 'requested' or promotion.get('review_id') != review['review_id']:
            raise Refused('PROMOTION_NOT_REQUESTED')
        if set(status.get('promote_blockers', [])) - {'ALREADY_REQUESTED'}:
            raise Refused('PROMOTION_BLOCKED')
        run = self.get('/api/runs/' + run_id)
        if run.get('status') not in ('active', 'completed'):
            raise Refused('RUN_NOT_ACTIVE')
        if run.get('base_commit') != request['base_commit'] or run.get('contract_id') != request['contract_id'] or run.get('contract_version') != request['contract_version']:
            raise Refused('RUN_MISMATCH')
        contract = self.get(f"/api/contracts/{request['contract_id']}/versions/{request['contract_version']}")
        if contract.get('status') != 'active' or contract.get('base_commit') != request['base_commit']:
            raise Refused('CONTRACT_NOT_ACTIVE')
        return key

    def promote(self, request):
        key = self.verify(request)
        artifact = self.artifacts.read(key)
        def state():
            return self.get('/api/runs/' + request['run_id']).get('status')
        return core.promote(self.repository, artifact, request['base_commit'], request['patch_sha256'], state)


def handler(verifier, token):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/promote' or not hmac.compare_digest(self.headers.get('X-IntentLock-Promoter-Token', ''), token):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 16384:
                    raise Refused('INVALID_REQUEST')
                request = json.loads(self.rfile.read(size))
                digest = verifier.promote(request)
                reply = {'status': 'promoted', 'base_commit': request['base_commit'], 'applied_patch_sha256': digest}
            except (Refused, core.PromotionRefused) as exc:
                reply = {'status': 'refused', 'reason_code': str(exc)[:64]}
            except Exception:
                reply = {'status': 'refused', 'reason_code': 'VERIFICATION_UNAVAILABLE'}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Handler


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    token = os.environ['INTENTLOCK_PROMOTER_TOKEN']
    if len(token) < 24:
        raise SystemExit('promoter token must be at least 24 characters')
    verifier = Verifier(args.repository, artifacts_module.from_env(), os.environ['INTENTLOCK_API_URL'], os.environ['INTENTLOCK_INTERNAL_TOKEN'])
    ThreadingHTTPServer(('127.0.0.1', args.port), handler(verifier, token)).serve_forever()
