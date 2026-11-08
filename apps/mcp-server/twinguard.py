"""MCP-facing TwinGuard preview and immutable evidence persistence."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone
import uuid

ROOT = Path(__file__).resolve().parents[2]
def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
local=_load('intentlock_local',ROOT/'packages/sandbox-provider/local.py')
impact=_load('intentlock_impact',ROOT/'packages/impact-analyzer/analyze.py')
artifacts=_load('intentlock_artifacts',ROOT/'packages/sandbox-provider/artifacts.py')

def preview(client, repository, run_id, contract_id, version, patch_text):
    if not isinstance(patch_text,str): raise ValueError('INVALID_ARGUMENTS')
    contract=client.request(f'/api/contracts/{contract_id}/versions/{version}')
    if contract['contract_id']!=contract_id or contract['version']!=version or contract['status']!='active': raise ValueError('CONTRACT_NOT_ACTIVE')
    if contract['approved_tests'].get('auth-tests',{}).get('argv') != ['npm','run','test:auth']: raise ValueError('TEST_NOT_APPROVED')
    base=local.clean_base(repository)
    if base!=contract['base_commit']: raise ValueError('STALE_BASE_COMMIT')
    result=local.execute(repository,base,patch_text.encode(),timeout=min(90,contract['approved_tests']['auth-tests']['timeout_seconds']+15))
    sandbox_id='sandbox_'+uuid.uuid4().hex[:12]
    evidence=impact.analyze(result,contract,run_id,sandbox_id)
    store=artifacts.from_env()
    store.put(f'runs/{run_id}/input/repository.tar.gz',local.archive_repository(repository,base))
    digest=evidence['patch_sha256']
    key=f'runs/{run_id}/patches/{digest}.patch'
    if store.put(key,evidence['patch']) != digest: raise ValueError('PATCH_HASH_MISMATCH')
    output_key=f'runs/{run_id}/tests/auth-tests_{sandbox_id}.txt'
    store.put(output_key,result['test_output'][:65536])
    report_id='impact_'+uuid.uuid4().hex[:12]
    report={'impact_report_id':report_id,'run_id':run_id,'sandbox_id':sandbox_id,
            'contract_id':contract_id,'contract_version':version,'base_commit':base,
            'patch_s3_key':key,'patch_sha256':digest,'summary':evidence['summary'],
            'tests':[{'test_id':'auth-tests','status':'passed' if result['test_exit']==0 else 'failed' if result['test_exit'] is not None else 'error',
                      'exit_code':result['test_exit'],'duration_ms':result['duration_ms'],'output_s3_key':output_key}],
            'findings':evidence['findings'],'decision':evidence['decision'],
            'created_at':datetime.now(timezone.utc).isoformat()}
    store.put(f'runs/{run_id}/impact/{report_id}.json',json.dumps(report,sort_keys=True,separators=(',',':')).encode())
    client.request('/internal/impact',report)
    return {'impact_report_id':report['impact_report_id'],'decision':report['decision'],
            'patch_sha256':digest,'findings':report['findings']}


def approved_test(client, repository, run_id, contract_id, version, test_id):
    contract = client.request(f'/api/contracts/{contract_id}/versions/{version}')
    if contract['contract_id'] != contract_id or contract['version'] != version or contract['status'] != 'active':
        raise ValueError('CONTRACT_NOT_ACTIVE')
    if test_id != 'auth-tests' or contract['approved_tests'].get(test_id, {}).get('argv') != ['npm', 'run', 'test:auth']:
        raise ValueError('TEST_NOT_APPROVED')
    base = local.clean_base(repository)
    if base != contract['base_commit']:
        raise ValueError('STALE_BASE_COMMIT')
    result = local.run_approved_test(repository, base, timeout=min(90, contract['approved_tests'][test_id]['timeout_seconds'] + 15))
    key = f'runs/{run_id}/tests/{test_id}_{uuid.uuid4().hex[:12]}.txt'
    artifacts.from_env().put(key, result['test_output'])
    return {'test_id': test_id, 'status': 'passed' if result['test_exit'] == 0 else 'failed',
            'exit_code': result['test_exit'], 'duration_ms': result['duration_ms'], 'output_s3_key': key}


def record_agent_note(client, run_id, category, data):
    if category not in ('permissions', 'summaries'):
        raise ValueError('INVALID_NOTE_CATEGORY')
    key = f'runs/{run_id}/agent/{category}/{uuid.uuid4().hex[:12]}.json'
    artifacts.from_env().put(key, json.dumps(data, sort_keys=True, separators=(',', ':')).encode())
    client.event(run_id, f'Agent {category} evidence: {key}')
    return {'evidence_key': key, 'access_granted': False}
