"""Real Docker: automatic check selection, coverage findings and the unchanged auth demo."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


impact_preview = module('impact_preview_under_test', BASE / 'apps/mcp-server/impact_preview.py')
checks = impact_preview.checks
CAT = checks.load_catalogue()
ALL_TESTS = {k: {'argv': v['argv'], 'timeout_seconds': v['timeout_seconds']} for k, v in CAT['checks'].items()}
LIMITS = {'max_files_changed': 8, 'max_files_deleted': 0, 'max_lines_added': 300, 'max_lines_deleted': 150}


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()


class CheckSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        shutil.copytree(BASE / 'demo/repository', self.repo, ignore=shutil.ignore_patterns('.DS_Store'))
        subprocess.run(['git', '-C', str(self.repo), 'init', '-q'], check=True)
        git(self.repo, 'config', 'user.email', 'demo@intentlock.invalid'); git(self.repo, 'config', 'user.name', 'IntentLock')
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-qm', 'baseline')
        self.base = git(self.repo, 'rev-parse', 'HEAD')

    def contract(self, allowed, tests=None):
        return {'base_commit': self.base, 'allowed_paths': allowed, 'protected_paths': ['.env*', '.git/**'],
                'limits': LIMITS, 'approved_tests': ALL_TESTS if tests is None else tests}

    def patch(self, relative, old, new, create=False):
        target = self.repo / relative
        original = None if create else target.read_text()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new if create else original.replace(old, new))
        if create: git(self.repo, 'add', '-N', relative)
        p = subprocess.check_output(['git', '-C', str(self.repo), 'diff'])
        if create: git(self.repo, 'rm', '-q', '--cached', relative); target.unlink()
        else: target.write_text(original)
        return p

    def evaluate(self, patch, contract):
        plan, result, evidence = impact_preview.evaluate(self.repo, self.base, patch, contract, 'run-test', 'sandbox-test')
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        return plan, result, evidence

    def codes(self, evidence):
        return [f['code'] for f in evidence['findings']]

    def outcome(self, result):
        return {c['id']: c['exit'] for c in result['checks']}

    def test_original_auth_fix_selects_auth_tests_and_passes(self):
        patch = self.patch('src/auth/session.ts', '>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        plan, result, evidence = self.evaluate(patch, self.contract(['src/auth/**']))
        self.assertEqual([c['id'] for c in plan['run']], ['auth-tests', 'typecheck'])
        self.assertEqual(self.outcome(result), {'auth-tests': 0, 'typecheck': 0})
        self.assertEqual(evidence['decision'], 'pass', evidence['findings'])

    def test_original_auth_bypass_is_still_blocked(self):
        patch = self.patch('src/auth/session.ts', "token === 'demo-valid-token'", 'true')
        _, result, evidence = self.evaluate(patch, self.contract(['src/auth/**']))
        self.assertNotEqual(self.outcome(result)['auth-tests'], 0)
        self.assertEqual(evidence['decision'], 'block')
        self.assertIn('CRITICAL_TEST_FAILURE', self.codes(evidence))

    def test_legacy_auth_only_contract_runs_only_auth_tests(self):
        patch = self.patch('src/auth/session.ts', '>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        plan, result, evidence = self.evaluate(patch, self.contract(['src/auth/**'], {'auth-tests': ALL_TESTS['auth-tests']}))
        self.assertEqual(self.outcome(result), {'auth-tests': 0})
        self.assertEqual(evidence['decision'], 'pass')
        self.assertIn('typecheck', plan['not_approved'])

    def test_payments_rounding_regression_selects_payments_tests_and_blocks(self):
        patch = self.patch('src/payments/calculator.ts', 'Math.round((amountCents * percent) / 100)', 'Math.floor((amountCents * percent) / 100)')
        plan, result, evidence = self.evaluate(patch, self.contract(['src/payments/**']))
        self.assertEqual([c['id'] for c in plan['run']], ['payments-tests', 'typecheck'])
        out = self.outcome(result)
        self.assertNotEqual(out['payments-tests'], 0)
        self.assertEqual(out['typecheck'], 0)
        self.assertEqual(evidence['decision'], 'block')
        failure = [f for f in evidence['findings'] if f['code'] == 'CRITICAL_TEST_FAILURE']
        self.assertEqual([f['evidence'] for f in failure], ['payments-tests'])
        self.assertNotIn('auth-tests', out)

    def test_type_error_is_caught_by_typecheck_even_if_tests_would_pass(self):
        patch = self.patch('src/payments/calculator.ts', 'return amountCents + Math.round', 'const unused: number = "oops";\n  return amountCents + Math.round')
        _, result, evidence = self.evaluate(patch, self.contract(['src/payments/**']))
        self.assertNotEqual(self.outcome(result)['typecheck'], 0)
        self.assertEqual(evidence['decision'], 'block')
        self.assertIn('CHECK_FAILED', self.codes(evidence))

    def test_correct_payments_change_passes(self):
        patch = self.patch('src/payments/calculator.ts', 'export function addTax', '/** Adds tax. */\nexport function addTax')
        _, result, evidence = self.evaluate(patch, self.contract(['src/payments/**']))
        self.assertEqual(self.outcome(result), {'payments-tests': 0, 'typecheck': 0})
        self.assertEqual(evidence['decision'], 'pass', evidence['findings'])

    def test_uncovered_path_is_never_a_silent_pass(self):
        patch = self.patch('src/utils/format.ts', '', 'export const fmt = (n: number): string => n.toFixed(2);\n', create=True)
        plan, result, evidence = self.evaluate(patch, self.contract(['src/utils/**']))
        self.assertEqual([c['id'] for c in plan['run']], ['typecheck'])
        self.assertEqual(self.outcome(result), {'typecheck': 0})
        self.assertIn('INSUFFICIENT_TEST_COVERAGE', self.codes(evidence))
        self.assertEqual(evidence['decision'], 'review_required')

    def test_uncovered_sensitive_path_is_blocked(self):
        # Contract approves only auth-tests, so payments source has no approved test -> sensitive and uncovered.
        patch = self.patch('src/payments/calculator.ts', 'export function addTax', '/** Adds tax. */\nexport function addTax')
        _, _, evidence = self.evaluate(patch, self.contract(['src/payments/**'], {'auth-tests': ALL_TESTS['auth-tests']}))
        finding = [f for f in evidence['findings'] if f['code'] == 'INSUFFICIENT_TEST_COVERAGE']
        self.assertEqual([f['severity'] for f in finding], ['high'])
        self.assertEqual(evidence['decision'], 'block')

    def test_docs_only_change_needs_no_tests(self):
        patch = self.patch('docs/NOTES.md', '', '# notes\n', create=True)
        plan, _, evidence = self.evaluate(patch, self.contract(['docs/**']))
        self.assertEqual(plan['uncovered'], [])
        self.assertNotIn('INSUFFICIENT_TEST_COVERAGE', self.codes(evidence))
        self.assertNotIn('TEST_NOT_RUN', self.codes(evidence))

    def test_editing_tsconfig_to_hide_errors_is_blocked_and_checks_do_not_run(self):
        patch = self.patch('tsconfig.json', '"strict":true', '"strict":false,"include":[]')
        _, result, evidence = self.evaluate(patch, self.contract(['tsconfig.json']))
        self.assertEqual(result['checks'], [])
        self.assertIn('TRUSTED_TEST_MODIFIED', self.codes(evidence))
        self.assertEqual(evidence['decision'], 'block')

    def test_editing_a_payments_test_is_still_blocked_before_any_check_runs(self):
        patch = self.patch('tests/payments/calculator.test.ts', 'assert.equal(applyDiscount(100, 0), 100);', 'assert.equal(applyDiscount(100, 0), 99);')
        _, result, evidence = self.evaluate(patch, self.contract(['tests/payments/**']))
        self.assertEqual(result['checks'], [])
        self.assertIn('TRUSTED_TEST_MODIFIED', self.codes(evidence))
        self.assertEqual(evidence['decision'], 'block')

    def test_changing_package_scripts_cannot_redirect_a_check(self):
        patch = self.patch('package.json', 'node --experimental-strip-types --test tests/payments/*.test.ts', 'node -e process.exit(0)')
        _, result, evidence = self.evaluate(patch, self.contract(['package.json']))
        self.assertEqual(result['checks'], [])
        self.assertEqual(evidence['decision'], 'block')

    def test_contract_with_a_tampered_command_fails_closed(self):
        patch = self.patch('src/auth/session.ts', '>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        bad = self.contract(['src/auth/**'], {'auth-tests': {'argv': ['npm', 'run', 'test:auth', '--', '--no-op'], 'timeout_seconds': 60}})
        with self.assertRaisesRegex(ValueError, 'TEST_NOT_APPROVED'):
            impact_preview.evaluate(self.repo, self.base, patch, bad, 'run-test', 'sandbox-test')


if __name__ == '__main__':
    unittest.main()
