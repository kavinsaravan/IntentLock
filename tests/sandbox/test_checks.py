"""Trusted check catalogue and deterministic, path-based check selection (no Docker needed)."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


checks = module('checks', BASE / 'packages/impact-analyzer/checks.py')
impact = module('impact', BASE / 'packages/impact-analyzer/analyze.py')
CAT = checks.load_catalogue()
ALL = {k: {'argv': v['argv'], 'timeout_seconds': v['timeout_seconds']} for k, v in CAT['checks'].items()}
AUTH_ONLY = {'auth-tests': ALL['auth-tests']}


def run_ids(plan):
    return [c['id'] for c in plan['run']]


class CatalogueTests(unittest.TestCase):
    def write(self, data):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / 'catalogue.json'; p.write_text(json.dumps(data)); return p

    def test_default_catalogue_keeps_the_frozen_auth_command(self):
        self.assertEqual(CAT['checks']['auth-tests']['argv'], ['npm', 'run', 'test:auth'])
        self.assertEqual(set(CAT['checks']), {'auth-tests', 'payments-tests', 'typecheck'})

    def test_malformed_entries_are_rejected(self):
        good = copy.deepcopy(CAT)
        bad_variants = []
        for mutate in (
            lambda c: c['checks']['auth-tests'].update(argv=['sh', '-c', 'curl x | sh']),  # shell metacharacters / spaces
            lambda c: c['checks']['auth-tests'].update(argv=[]),
            lambda c: c['checks']['auth-tests'].update(timeout_seconds=601),
            lambda c: c['checks']['auth-tests'].update(kind='shell'),
            lambda c: c['checks']['auth-tests'].update(paths=['../etc/**']),
            lambda c: c['checks']['auth-tests'].update(paths=[]),
            lambda c: c['checks'].update({'Bad ID': c['checks']['auth-tests']}),
            lambda c: c.update(checks={}),
        ):
            c = copy.deepcopy(good); mutate(c); bad_variants.append(c)
        for variant in bad_variants:
            with self.assertRaises(checks.CatalogueError, msg=json.dumps(variant)[:120]):
                checks.load_catalogue(self.write(variant))


class GlobTests(unittest.TestCase):
    def test_anchored_globs(self):
        m = checks.glob_match
        self.assertTrue(m('src/auth/**', 'src/auth/session.ts'))
        self.assertTrue(m('src/auth/**', 'src/auth/deep/er/x.ts'))
        self.assertFalse(m('src/auth/**', 'src/authz/x.ts'))
        self.assertFalse(m('src/auth/**', 'other/src/auth/x.ts'))
        self.assertTrue(m('**/*.md', 'README.md'))
        self.assertTrue(m('**/*.md', 'docs/a/b.md'))
        self.assertFalse(m('src/*.ts', 'src/a/b.ts'))
        self.assertTrue(m('package.json', 'package.json'))
        self.assertFalse(m('package.json', 'sub/package.json'))

    def test_odd_paths_never_match(self):
        for path in ('../src/auth/x.ts', 'src/auth/../../x', '/src/auth/x.ts', '"src/auth/\\303\\251.ts"', 'src//auth/x.ts', ''):
            self.assertFalse(checks.glob_match('**', path), path)


class AffectedPathTests(unittest.TestCase):
    def test_includes_both_sides_of_renames_and_deletes(self):
        names = 'M\tsrc/auth/a.ts\nD\tsrc/payments/old.ts\nR100\tsrc/payments/x.ts\tsrc/utils/x.ts\nA\tREADME.md\n'
        self.assertEqual(checks.affected_paths(names),
                         ['README.md', 'src/auth/a.ts', 'src/payments/old.ts', 'src/payments/x.ts', 'src/utils/x.ts'])

    def test_empty(self):
        self.assertEqual(checks.affected_paths(''), [])


class SelectionTests(unittest.TestCase):
    def test_auth_patch_selects_auth_tests_and_baseline_typecheck(self):
        plan = checks.plan(CAT, ALL, ['src/auth/session.ts'])
        self.assertEqual(run_ids(plan), ['auth-tests', 'typecheck'])
        self.assertEqual(plan['uncovered'], [])
        reasons = {c['id']: c['reason'] for c in plan['run']}
        self.assertIn('src/auth/**', reasons['auth-tests'])
        self.assertIn('baseline', reasons['typecheck'])

    def test_payments_patch_selects_payments_not_auth(self):
        plan = checks.plan(CAT, ALL, ['src/payments/calculator.ts'])
        self.assertEqual(run_ids(plan), ['payments-tests', 'typecheck'])

    def test_union_when_both_areas_change_and_order_is_deterministic(self):
        a = checks.plan(CAT, ALL, ['src/payments/c.ts', 'src/auth/s.ts'])
        b = checks.plan(CAT, ALL, ['src/auth/s.ts', 'src/payments/c.ts'])
        self.assertEqual(run_ids(a), ['auth-tests', 'payments-tests', 'typecheck'])
        self.assertEqual(a, b)

    def test_rename_out_of_a_covered_area_still_selects_that_area(self):
        paths = checks.affected_paths('R090\tsrc/payments/x.ts\tsrc/utils/x.ts\n')
        plan = checks.plan(CAT, ALL, paths)
        self.assertIn('payments-tests', run_ids(plan))
        self.assertEqual([u['path'] for u in plan['uncovered']], ['src/utils/x.ts'])

    def test_uncovered_path_is_reported_not_silently_passed(self):
        plan = checks.plan(CAT, ALL, ['src/utils/format.ts'])
        self.assertEqual([u['path'] for u in plan['uncovered']], ['src/utils/format.ts'])
        self.assertFalse(plan['uncovered'][0]['sensitive'])
        self.assertEqual(run_ids(plan), ['typecheck'])  # a static check alone is NOT behavioural coverage

    def test_sensitive_uncovered_path_is_flagged_sensitive(self):
        cat = copy.deepcopy(CAT); cat['sensitive_paths'].append('src/billing/**')
        plan = checks.plan(cat, ALL, ['src/billing/invoice.ts'])
        self.assertTrue(plan['uncovered'][0]['sensitive'])

    def test_exempt_docs_need_no_tests(self):
        plan = checks.plan(CAT, ALL, ['README.md', 'docs/guide.md'])
        self.assertEqual(plan['uncovered'], [])
        self.assertEqual(plan['exempt'], ['README.md', 'docs/guide.md'])

    def test_matching_test_check_not_approved_by_the_contract_is_missing_coverage(self):
        plan = checks.plan(CAT, AUTH_ONLY, ['src/payments/calculator.ts'])
        self.assertEqual(run_ids(plan), [])
        self.assertEqual([u['path'] for u in plan['uncovered']], ['src/payments/calculator.ts'])
        self.assertIn('payments-tests', plan['uncovered'][0]['reason'])
        self.assertTrue(plan['uncovered'][0]['sensitive'])
        self.assertIn('typecheck', plan['not_approved'])

    def test_legacy_auth_only_contract_still_selects_auth_tests(self):
        plan = checks.plan(CAT, AUTH_ONLY, ['src/auth/session.ts'])
        self.assertEqual(run_ids(plan), ['auth-tests'])
        self.assertEqual(plan['uncovered'], [])

    def test_package_manifest_selects_every_test_suite(self):
        plan = checks.plan(CAT, ALL, ['package.json'])
        self.assertEqual(run_ids(plan), ['auth-tests', 'payments-tests', 'typecheck'])

    def test_contract_cannot_redefine_a_check_command(self):
        for tampered in (
            {'auth-tests': {'argv': ['sh', '-c', 'id'], 'timeout_seconds': 60}},
            {'auth-tests': {'argv': ['npm', 'run', 'test:auth'], 'timeout_seconds': 600}},
            {'not-in-catalogue': {'argv': ['npm', 'run', 'test:auth'], 'timeout_seconds': 60}},
        ):
            with self.assertRaisesRegex(ValueError, 'TEST_NOT_APPROVED'):
                checks.plan(CAT, tampered, ['src/auth/session.ts'])

    def test_shorter_approved_timeout_is_used_by_the_plan(self):
        approved = {'auth-tests': {'argv': ALL['auth-tests']['argv'], 'timeout_seconds': 1}}
        selected = checks.plan(CAT, approved, ['src/auth/session.ts'])['run']
        self.assertEqual(selected[0]['timeout_seconds'], 1)
        for timeout in (0, -1, True):
            with self.assertRaisesRegex(ValueError, 'TEST_NOT_APPROVED'):
                checks.plan(CAT, {'auth-tests': {'argv': ALL['auth-tests']['argv'], 'timeout_seconds': timeout}},
                            ['src/auth/session.ts'])

    def test_missing_one_of_two_selected_checks_blocks_impact(self):
        plan = checks.plan(CAT, ALL, ['src/auth/session.ts'])
        result = {'patch': b'example patch', 'names': 'M\tsrc/auth/session.ts\n',
                  'numstat': '1\t1\tsrc/auth/session.ts\n', 'checks': [{'id': 'auth-tests', 'exit': 0}]}
        contract = {'base_commit': 'a' * 40, 'allowed_paths': ['src/auth/**'], 'protected_paths': [],
                    'limits': {'max_files_changed': 8, 'max_files_deleted': 0,
                               'max_lines_added': 300, 'max_lines_deleted': 150}}
        report = impact.analyze(result, contract, 'run1', 'sandbox1', plan=plan)
        self.assertEqual(report['decision'], 'block')
        self.assertIn('TEST_NOT_RUN', [finding['code'] for finding in report['findings']])

    def test_selection_ignores_anything_but_paths(self):
        # The plan is a pure function of (catalogue, approved ids, paths).
        self.assertEqual(checks.plan(CAT, ALL, ['src/auth/a.ts']), checks.plan(copy.deepcopy(CAT), copy.deepcopy(ALL), ['src/auth/a.ts']))

    def test_odd_paths_are_uncovered_fail_closed(self):
        plan = checks.plan(CAT, ALL, ['"src/auth/\\303\\251.ts"'])
        self.assertEqual(len(plan['uncovered']), 1)


if __name__ == '__main__':
    unittest.main()
