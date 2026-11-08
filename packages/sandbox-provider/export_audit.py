"""Finalize a terminal run's redacted control-plane evidence in S3/local artifacts."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps/mcp-server'))
from control_plane import Client

spec = importlib.util.spec_from_file_location('intentlock_artifacts', Path(__file__).with_name('artifacts.py'))
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def export(client, store, run_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise ValueError('INVALID_RUN_ID')
    run = client.run(run_id)
    if run['status'] not in ('completed', 'failed', 'blocked', 'stopped'):
        raise ValueError('RUN_NOT_TERMINAL')
    contract = client.request(f"/api/contracts/{run['contract_id']}/versions/{run['contract_version']}")
    events = client.request(f'/api/runs/{run_id}/events')['events']
    impact_ids = sorted({event['details']['impact_report_id'] for event in events
                         if event['event_type'] == 'impact_report' and isinstance(event.get('details'), dict)
                         and isinstance(event['details'].get('impact_report_id'), str)})
    impacts = []
    for impact_id in impact_ids:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', impact_id):
            raise ValueError('INVALID_IMPACT_ID')
        impacts.append({'report': client.request('/api/impact/' + impact_id),
                        'status': client.request('/api/impact/' + impact_id + '/status')})
    if run['status'] == 'completed':
        for item in impacts:
            report, state = item['report'], item['status']
            review = state.get('review') or {}
            promotion = state.get('promotion') or {}
            if report.get('decision') != 'block' and review.get('decision') != 'reject' and promotion.get('status') not in ('promoted', 'refused', 'failed'):
                raise ValueError('RUN_REVIEW_PENDING')
    record = {'run': run, 'contract': contract, 'events': events, 'impacts': impacts}
    data = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
    key = f'runs/{run_id}/audit/final.json'
    digest = store.put(key, data)
    return key, digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    key, digest = export(Client.from_env(), artifacts.from_env(), args.run_id)
    print(f'{key} sha256={digest}')
