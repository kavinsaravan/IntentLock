"""MCP-facing TwinGuard preview and evidence persistence for local demo."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import uuid

ROOT = Path(__file__).resolve().parents[2]
def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
local=_load('intentlock_local',ROOT/'packages/sandbox-provider/local.py')
impact=_load('intentlock_impact',ROOT/'packages/impact-analyzer/analyze.py')

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
    artifact_root=os.environ.get('INTENTLOCK_ARTIFACT_DIR')
    if not artifact_root: raise ValueError('ARTIFACT_DIR_NOT_CONFIGURED')
    artifact_root=Path(artifact_root).resolve()
    digest=evidence['patch_sha256']
    key=f'runs/{run_id}/patches/{digest}.patch'
    path=artifact_root/key
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(evidence['patch'])
    output_key=f'runs/{run_id}/tests/auth-tests.txt'
    output=artifact_root/output_key
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(result['test_output'][:65536])
    report={'impact_report_id':'impact_'+uuid.uuid4().hex[:12],'run_id':run_id,'sandbox_id':sandbox_id,
            'contract_id':contract_id,'contract_version':version,'base_commit':base,
            'patch_s3_key':key,'patch_sha256':digest,'summary':evidence['summary'],
            'tests':[{'test_id':'auth-tests','status':'passed' if result['test_exit']==0 else 'failed' if result['test_exit'] is not None else 'error',
                      'exit_code':result['test_exit'],'duration_ms':result['duration_ms'],'output_s3_key':output_key}],
            'findings':evidence['findings'],'decision':evidence['decision'],
            'created_at':datetime.now(timezone.utc).isoformat()}
    client.request('/internal/impact',report)
    return {'impact_report_id':report['impact_report_id'],'decision':report['decision'],
            'patch_sha256':digest,'findings':report['findings']}
