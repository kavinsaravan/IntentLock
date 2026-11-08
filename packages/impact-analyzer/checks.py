"""Trusted check catalogue and deterministic, path-based check selection.

The catalogue is administrator-owned (this file's sibling `catalogue.json`, or the file named by
INTENTLOCK_CHECK_CATALOGUE in the trusted MCP-server process). It never lives in the agent's
workspace. A mission contract may only *reference* catalogue ids; it can never redefine a command.
Selection is a pure function of (catalogue, contract-approved ids, normalized changed paths), so
neither the agent nor Bedrock can steer which checks run.
"""
import json
import os
import re
from pathlib import Path, PurePosixPath

DEFAULT_PATH = Path(__file__).with_name('catalogue.json')
ID_RX = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# argv words are written to a whitespace-separated file read by the sandbox runner: no spaces/metacharacters.
ARG_RX = re.compile(r'^[A-Za-z0-9_./:=@+-]{1,128}$')
KINDS = ('test', 'static')


class CatalogueError(Exception):
    pass


def _pattern(p):
    if not isinstance(p, str) or not p or p.startswith('/') or '..' in PurePosixPath(p).parts or '\\' in p or '\x00' in p:
        raise CatalogueError(f'invalid path pattern {p!r}')
    return p


def load_catalogue(path=None):
    path = Path(path or os.environ.get('INTENTLOCK_CHECK_CATALOGUE') or DEFAULT_PATH)
    try:
        cat = json.loads(path.read_text())
        checks = cat['checks']
        if not isinstance(checks, dict) or not checks:
            raise CatalogueError('catalogue has no checks')
        for cid, c in checks.items():
            if not isinstance(cid, str) or not ID_RX.match(cid):
                raise CatalogueError(f'invalid check id {cid!r}')
            if c.get('kind') not in KINDS:
                raise CatalogueError(f'{cid}: kind must be one of {KINDS}')
            argv = c.get('argv')
            if not isinstance(argv, list) or not argv or len(argv) > 16 or not all(isinstance(a, str) and ARG_RX.match(a) for a in argv):
                raise CatalogueError(f'{cid}: argv must be 1-16 plain words')
            t = c.get('timeout_seconds')
            if not isinstance(t, int) or isinstance(t, bool) or not 0 < t <= 600:
                raise CatalogueError(f'{cid}: timeout_seconds must be 1-600')
            if not isinstance(c.get('paths'), list) or not c['paths']:
                raise CatalogueError(f'{cid}: paths required')
            for p in c['paths']:
                _pattern(p)
            if not isinstance(c.get('always', False), bool):
                raise CatalogueError(f'{cid}: always must be boolean')
        for key in ('sensitive_paths', 'exempt_paths'):
            cat.setdefault(key, [])
            if not isinstance(cat[key], list):
                raise CatalogueError(f'{key} must be a list')
            for p in cat[key]:
                _pattern(p)
    except CatalogueError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise CatalogueError(f'cannot load check catalogue: {e}') from e
    return cat


def _regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out.append('(?:.*/)?'); i += 3
        elif pattern.startswith('/**', i) and i + 3 == len(pattern):
            out.append('/.*'); i += 3
        elif pattern.startswith('**', i):
            out.append('.*'); i += 2
        elif pattern[i] == '*':
            out.append('[^/]*'); i += 1
        elif pattern[i] == '?':
            out.append('[^/]'); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile('^' + ''.join(out) + '$')


def glob_match(pattern, path):
    """Repo-root-anchored glob. Odd paths (absolute, `..`, quoted, empty segments) never match."""
    if not path or path.startswith(('/', '"')) or '\\' in path or '\x00' in path:
        return False
    parts = path.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return False
    return bool(_regex(pattern).match(path))


def affected_paths(names_text):
    """Every path named by `git diff --name-status`: both sides of renames, deletes included."""
    paths = set()
    for line in names_text.splitlines():
        cols = line.split('\t')
        if len(cols) >= 2:
            paths.update(c for c in cols[1:] if c)
    return sorted(paths)


def _first(patterns, path):
    return next((p for p in patterns if glob_match(p, path)), None)


def plan(catalogue, contract_tests, paths):
    """Decide which catalogue checks to run for `paths`.

    `contract_tests` is the contract's approved_tests mapping. Each entry must equal the catalogue
    definition (argv exact, timeout no larger) or the whole preview fails closed with TEST_NOT_APPROVED.
    """
    cat_checks = catalogue['checks']
    for cid, spec in contract_tests.items():
        c = cat_checks.get(cid)
        if c is None or spec.get('argv') != c['argv'] or type(spec.get('timeout_seconds')) is not int \
                or not 0 < spec['timeout_seconds'] <= c['timeout_seconds']:
            raise ValueError('TEST_NOT_APPROVED')
    approved = set(contract_tests)
    run, reasons = {}, {}
    for cid in sorted(cat_checks):
        c = cat_checks[cid]
        if cid not in approved:
            continue
        if c.get('always'):
            reasons[cid] = 'baseline check (always runs)'
        for path in paths:
            pat = _first(c['paths'], path)
            if pat and cid not in reasons:
                reasons[cid] = f'matched {pat} (changed: {path})'
    for cid in sorted(reasons):
        c = cat_checks[cid]
        run[cid] = {'id': cid, 'argv': list(c['argv']), 'timeout_seconds': contract_tests[cid]['timeout_seconds'], 'kind': c['kind'], 'reason': reasons[cid]}
    uncovered, exempt = [], []
    for path in paths:
        if _first(catalogue['exempt_paths'], path):
            exempt.append(path); continue
        if any(cat_checks[cid]['kind'] == 'test' and _first(cat_checks[cid]['paths'], path) for cid in run):
            continue
        # Would a test check cover this path if the contract had approved it?
        missing = [cid for cid in sorted(cat_checks) if cid not in approved and cat_checks[cid]['kind'] == 'test' and _first(cat_checks[cid]['paths'], path)]
        reason = f"matching test check(s) {', '.join(missing)} not approved by the contract" if missing else 'no trusted test covers this path'
        uncovered.append({'path': path, 'sensitive': bool(_first(catalogue['sensitive_paths'], path)), 'reason': reason})
    not_approved = sorted(cid for cid in cat_checks if cid not in approved)
    return {'run': [run[cid] for cid in sorted(run)], 'uncovered': uncovered, 'exempt': exempt, 'not_approved': not_approved}
