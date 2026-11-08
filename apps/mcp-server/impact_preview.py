"""MCP-facing patch preview and immutable evidence persistence."""
import importlib.util
import json
import re
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
checks=_load('intentlock_checks',ROOT/'packages/impact-analyzer/checks.py')

def evaluate(repository, base, patch, contract, run_id, sandbox_id, catalogue=None):
    """Select checks from the changed paths (trusted code only), run them offline, analyze.

    Two sandbox passes: the first applies the patch and reports the normalized changed paths; selection
    runs on the host from those paths; the second applies the same patch again and runs exactly the
    selected catalogue commands. The patch bytes must be identical across both passes.
    """
    catalogue = catalogue or checks.load_catalogue()
    checks.plan(catalogue, contract['approved_tests'], [])  # fail closed on any tampered/unknown test before running anything
    dry = local.execute(repository, base, patch, run_test=False, timeout=60)
    plan = checks.plan(catalogue, contract['approved_tests'], checks.affected_paths(dry['names']))
    if plan['run']:
        result = local.execute(repository, base, patch, checks=plan['run'], timeout=min(300, 30 + sum(c['timeout_seconds'] for c in plan['run'])))
        if result['patch'] != dry['patch']: raise ValueError('PATCH_NONDETERMINISTIC')
    else:
        result = {**dry, 'checks': []}
    return plan, result, impact.analyze(result, contract, run_id, sandbox_id, plan=plan)


def preview(client, repository, run_id, contract_id, version, patch_text):
    if not isinstance(patch_text,str): raise ValueError('INVALID_ARGUMENTS')
    contract=client.request(f'/api/contracts/{contract_id}/versions/{version}')
    if contract['contract_id']!=contract_id or contract['version']!=version or contract['status']!='active': raise ValueError('CONTRACT_NOT_ACTIVE')
    base=local.clean_base(repository)
    if base!=contract['base_commit']: raise ValueError('STALE_BASE_COMMIT')
    sandbox_id='sandbox_'+uuid.uuid4().hex[:12]
    plan,result,evidence=evaluate(repository,base,patch_text.encode(),contract,run_id,sandbox_id)
    store=artifacts.from_env()
    store.put(f'runs/{run_id}/input/repository.tar.gz',local.archive_repository(repository,base))
    digest=evidence['patch_sha256']
    key=f'runs/{run_id}/patches/{digest}.patch'
    if store.put(key,evidence['patch']) != digest: raise ValueError('PATCH_HASH_MISMATCH')
    reasons={c['id']:c['reason'] for c in plan['run']}
    tests=[]
    for c in result['checks']:
        output_key=f"runs/{run_id}/tests/{c['id']}_{sandbox_id}.txt"
        store.put(output_key,c['output'])
        tests.append({'test_id':c['id'],'status':c['status'],'exit_code':c['exit'],'duration_ms':c['duration_ms'],
                      'output_s3_key':output_key,'reason':reasons[c['id']]})
    report_id='impact_'+uuid.uuid4().hex[:12]
    report={'impact_report_id':report_id,'run_id':run_id,'sandbox_id':sandbox_id,
            'contract_id':contract_id,'contract_version':version,'base_commit':base,
            'patch_s3_key':key,'patch_sha256':digest,'summary':evidence['summary'],
            'tests':tests,'findings':evidence['findings'],'decision':evidence['decision'],
            'coverage':{'selected_checks':[c['id'] for c in plan['run']],
                        'uncovered_paths':[u['path'] for u in plan['uncovered']],
                        'not_approved_checks':plan['not_approved']},
            'created_at':datetime.now(timezone.utc).isoformat()}
    store.put(f'runs/{run_id}/impact/{report_id}.json',json.dumps(report,sort_keys=True,separators=(',',':')).encode())
    client.request('/internal/impact',report)
    return {'impact_report_id':report['impact_report_id'],'decision':report['decision'],
            'patch_sha256':digest,'findings':report['findings']}


_COMMAND_ID_RX = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')  # same shape the policy engine enforces


def approved_check_ids(client, contract_id, version, contract=None):
    """IDs the agent may name, split by kind, for the tool descriptions.

    Advertising only: a call is still fully re-validated in `_run_catalogue_check`. An ID is listed only when
    the active contract approved it AND the trusted catalogue defines it with the identical argv, so contract
    text can never inject arbitrary strings into what the agent reads. Raises on any lookup failure; callers
    decide whether to fall back.
    """
    contract = contract if contract is not None else client.request(f'/api/contracts/{contract_id}/versions/{version}')
    if contract.get('contract_id') != contract_id or contract.get('version') != version or contract.get('status') != 'active':
        return {'test': [], 'static': []}
    catalogue = checks.load_catalogue()
    found = {'test': [], 'static': []}
    for check_id, approved in sorted(contract['approved_tests'].items()):
        entry = catalogue['checks'].get(check_id)
        if entry and _COMMAND_ID_RX.fullmatch(check_id) and approved.get('argv') == entry['argv'] and entry['kind'] in found:
            found[entry['kind']].append(check_id)
    return found


def _run_catalogue_check(client, repository, run_id, contract_id, version, check_id, *, only_kind=None):
    """Run ONE catalogue command by ID on the clean base snapshot (no agent patch), offline.

    The agent only ever supplies an ID. The argv/timeout come from the administrator catalogue, and the
    contract must have approved exactly that definition; anything else fails closed before Docker starts.
    """
    if not isinstance(check_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', check_id):
        raise ValueError('TEST_NOT_APPROVED')
    contract = client.request(f'/api/contracts/{contract_id}/versions/{version}')
    if contract['contract_id'] != contract_id or contract['version'] != version or contract['status'] != 'active':
        raise ValueError('CONTRACT_NOT_ACTIVE')
    catalogue = checks.load_catalogue()
    checks.plan(catalogue, contract['approved_tests'], [])  # any tampered/unknown contract entry fails closed
    entry = catalogue['checks'].get(check_id)
    if entry is None or check_id not in contract['approved_tests'] or (only_kind and entry['kind'] != only_kind):
        raise ValueError('TEST_NOT_APPROVED')
    base = local.clean_base(repository)
    if base != contract['base_commit']:
        raise ValueError('STALE_BASE_COMMIT')
    approved_timeout = contract['approved_tests'][check_id]['timeout_seconds']
    check = {'id': check_id, 'argv': entry['argv'], 'timeout_seconds': approved_timeout}
    result = local.run_check(repository, base, check, timeout=min(90, approved_timeout + 15))
    key = f'runs/{run_id}/tests/{check_id}_{uuid.uuid4().hex[:12]}.txt'
    artifacts.from_env().put(key, result['test_output'])
    code = result['test_exit']
    return {'kind': entry['kind'], 'status': 'passed' if code == 0 else 'timeout' if code in (124, 137, 143) else 'failed',
            'exit_code': code, 'duration_ms': result['duration_ms'], 'output_s3_key': key}


def run_command(client, repository, run_id, contract_id, version, command_id):
    """run_approved_command(command_id): any catalogue check (test or static) the contract approves."""
    r = _run_catalogue_check(client, repository, run_id, contract_id, version, command_id)
    return {'command_id': command_id, **r}


def approved_test(client, repository, run_id, contract_id, version, test_id):
    """run_approved_test(test_id): unchanged response shape; now any approved *test*-kind catalogue check."""
    r = _run_catalogue_check(client, repository, run_id, contract_id, version, test_id, only_kind='test')
    return {'test_id': test_id, 'status': 'passed' if r['exit_code'] == 0 else 'failed', 'exit_code': r['exit_code'],
            'duration_ms': r['duration_ms'], 'output_s3_key': r['output_s3_key']}


def record_agent_note(client, run_id, category, data):
    if category not in ('permissions', 'summaries'):
        raise ValueError('INVALID_NOTE_CATEGORY')
    key = f'runs/{run_id}/agent/{category}/{uuid.uuid4().hex[:12]}.json'
    artifacts.from_env().put(key, json.dumps(data, sort_keys=True, separators=(',', ':')).encode())
    client.event(run_id, f'Agent {category} evidence: {key}')
    return {'evidence_key': key, 'access_granted': False}
