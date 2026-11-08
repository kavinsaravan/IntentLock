"""Deterministic inspection of a Docker sandbox result."""
import hashlib
import fnmatch
import json
from pathlib import PurePosixPath

DEPENDENCY_FIELDS = ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')

def dependency_only(result):
    try:
        before = json.loads(result['package_before'])
        after = json.loads(result['package_after'])
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        return {k: v for k, v in before.items() if k not in DEPENDENCY_FIELDS} == \
               {k: v for k, v in after.items() if k not in DEPENDENCY_FIELDS}
    except (KeyError, TypeError, ValueError):
        return False

def trusted(path):
    parts = PurePosixPath(path).parts
    return any(p in ('test','tests','__tests__') for p in parts) or any(p.endswith(('.test.ts','.spec.ts','.test.js','.spec.js')) for p in parts) or path.startswith('jest.config.') or path.startswith('vitest.config.') or any(p.startswith('tsconfig') and p.endswith('.json') for p in parts)

def analyze(result, contract, run_id, sandbox_id, plan=None):
    """`plan` (from checks.plan) switches on catalogue-driven checks and coverage findings; without it the
    legacy single fixed auth test semantics apply."""
    patch = result['patch']; digest = hashlib.sha256(patch).hexdigest()
    changed = [(x[0], x[1]) for line in result['names'].splitlines() if (x := line.split('\t',1)) and len(x)==2]
    summary = {'files_created':sum(s=='A' for s,p in changed), 'files_modified':sum(s=='M' for s,p in changed), 'files_deleted':sum(s=='D' for s,p in changed), 'files_renamed':sum(s.startswith('R') for s,p in changed), 'lines_added':0, 'lines_deleted':0}
    for line in result['numstat'].splitlines():
        cols=line.split('\t')
        if len(cols)==3:
            summary['lines_added'] += int(cols[0]) if cols[0].isdigit() else 0
            summary['lines_deleted'] += int(cols[1]) if cols[1].isdigit() else 0
    findings=[]
    for status,path in changed:
        if not any(fnmatch.fnmatchcase(path, pat) or (pat.endswith('/**') and path.startswith(pat[:-3]+'/')) for pat in contract['allowed_paths']): findings.append({'severity':'critical','code':'PATH_NOT_ALLOWED','message':'Changed path outside contract','evidence':path})
        if trusted(path) or (path == 'package.json' and not dependency_only(result)):
            findings.append({'severity':'critical','code':'TRUSTED_TEST_MODIFIED','message':'Trusted test or test configuration changed','evidence':path})
        elif path == 'package.json':
            findings.append({'severity':'medium','code':'DEPENDENCY_CHANGE','message':'Dependency change requires human review','evidence':path})
        if path.startswith(('.env','.git/','.github/','infra/')) or any(fnmatch.fnmatchcase(path, pat) for pat in contract.get('protected_paths', [])): findings.append({'severity':'critical','code':'PROTECTED_PATH','message':'Protected path changed','evidence':path})
    if not patch: findings.append({'severity':'critical','code':'EMPTY_PATCH','message':'Patch produced no changes'})
    blocked_by_trusted_change = any(f['code']=='TRUSTED_TEST_MODIFIED' for f in findings)
    if plan is None:
        if result['test_exit'] not in (None,0): findings.append({'severity':'critical','code':'CRITICAL_TEST_FAILURE','message':'Approved authentication tests failed','evidence':'auth-tests'})
        if result['test_exit'] is None and not blocked_by_trusted_change: findings.append({'severity':'critical','code':'TEST_NOT_RUN','message':'Approved tests did not run'})
    else:
        recorded = result.get('checks', [])
        ran = {c['id']: c for c in recorded}
        selected_ids = {c['id'] for c in plan['run']}
        if (len(ran) != len(recorded) or set(ran) != selected_ids) and not blocked_by_trusted_change:
            findings.append({'severity':'critical','code':'TEST_NOT_RUN','message':'Selected checks did not all report results',
                             'evidence':', '.join(sorted(selected_ids - set(ran)))})
        for sel in plan['run']:
            c = ran.get(sel['id'])
            if c is not None and c['exit'] != 0:
                if sel['kind'] == 'test': findings.append({'severity':'critical','code':'CRITICAL_TEST_FAILURE','message':'Approved check failed: '+sel['id'],'evidence':sel['id']})
                else: findings.append({'severity':'critical','code':'CHECK_FAILED','message':'Approved check failed: '+sel['id'],'evidence':sel['id']})
        # "No failures" is not safety: a changed path with no relevant trusted test must be surfaced, never silently passed.
        for u in plan['uncovered']:
            if trusted(u['path']): continue  # already blocked as TRUSTED_TEST_MODIFIED
            findings.append({'severity':'high' if u['sensitive'] else 'medium','code':'INSUFFICIENT_TEST_COVERAGE','message':'No trusted test covers this change: '+u['reason'],'evidence':u['path']})
    limits=contract['limits']
    for field,key in [('files_deleted','max_files_deleted'),('lines_added','max_lines_added'),('lines_deleted','max_lines_deleted')]:
        if summary[field] > limits[key]: findings.append({'severity':'critical','code':'PATCH_LIMIT_EXCEEDED','message':key+' exceeded'})
    if sum(summary[k] for k in ('files_created','files_modified','files_deleted','files_renamed')) > limits['max_files_changed']: findings.append({'severity':'critical','code':'PATCH_LIMIT_EXCEEDED','message':'max_files_changed exceeded'})
    decision = 'block' if any(f['severity'] in ('critical', 'high') for f in findings) else 'review_required' if findings else 'pass'
    return {'run_id':run_id,'sandbox_id':sandbox_id,'base_commit':contract['base_commit'],'patch_sha256':digest,'summary':summary,'findings':findings,'decision':decision,'patch':patch}
