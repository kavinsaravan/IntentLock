"""Real Docker isolation and exact-patch behavior on a disposable demo Git repo."""
import importlib.util
import gzip
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[2]
def module(name,path):
    s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
local=module('local',BASE/'packages/sandbox-provider/local.py')
impact=module('impact',BASE/'packages/impact-analyzer/analyze.py')
promoter=module('promoter',BASE/'packages/patch-promoter/promote.py')

def git(repo,*args): return subprocess.check_output(['git','-C',str(repo),*args]).decode().strip()
class TwinGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name)/'repo';shutil.copytree(BASE/'demo/repository',self.repo)
        subprocess.run(['git','-C',str(self.repo),'init','-q'],check=True)
        git(self.repo,'config','user.email','demo@intentlock.invalid');git(self.repo,'config','user.name','IntentLock')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','baseline')
        self.base=git(self.repo,'rev-parse','HEAD')
        self.contract={'base_commit':self.base,'allowed_paths':['src/auth/**'], 'limits':{'max_files_changed':8,'max_files_deleted':0,'max_lines_added':300,'max_lines_deleted':150}}

    def patch(self,old,new):
        file=self.repo/'src/auth/session.ts';original=file.read_text();file.write_text(original.replace(old,new));p=subprocess.check_output(['git','-C',str(self.repo),'diff']);file.write_text(original);return p

    def test_docker_has_no_network_and_read_only_root(self):
        proof = local.probe_isolation()
        self.assertEqual(proof['interfaces'], ['lo'])
        self.assertTrue(proof['writeDenied'])
        self.assertIn(proof['networkError'], ('ENETUNREACH', 'EHOSTUNREACH'))

    def test_repository_archive_is_stable_and_rejects_credentials(self):
        one=local.archive_repository(self.repo,self.base)
        self.assertEqual(one,local.archive_repository(self.repo,self.base))
        self.assertIn(b'src/auth/session.ts',gzip.decompress(one))
        (self.repo/'.env').write_text('synthetic=secret\n')
        git(self.repo,'add','.env');git(self.repo,'commit','-qm','tracked secret')
        with self.assertRaises(local.SandboxError):
            local.archive_repository(self.repo,git(self.repo,'rev-parse','HEAD'))

    def test_safe_patch_only_promotes_after_review(self):
        patch=self.patch('>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        result=local.execute(self.repo,self.base,patch)
        report=impact.analyze(result,self.contract,'run-test','sandbox-test')
        self.assertEqual(report['decision'],'pass')
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
        artifact=Path(self.temp.name)/'reviewed.patch';artifact.write_bytes(report['patch'])
        self.assertEqual(promoter.promote(self.repo,artifact,self.base,report['patch_sha256'],lambda:'active'),report['patch_sha256'])
        self.assertIn('timeoutSeconds * 1000', (self.repo/'src/auth/session.ts').read_text())

    def test_unsafe_patch_blocks_and_preserves_repository(self):
        patch=self.patch("token === 'demo-valid-token'", 'true')
        result=local.execute(self.repo,self.base,patch)
        report=impact.analyze(result,self.contract,'run-test','sandbox-test')
        self.assertEqual(report['decision'],'block')
        self.assertIn('CRITICAL_TEST_FAILURE',[f['code'] for f in report['findings']])
        self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_trusted_test_and_script_changes_are_blocked_inside_twinguard(self):
        for relative, before, after in (
            ('tests/auth/session.test.ts', "verifyToken('invalid')", "verifyToken('demo-valid-token')"),
            ('package.json', 'node --experimental-strip-types', 'node --no-warnings --experimental-strip-types'),
        ):
            with self.subTest(relative=relative):
                target = self.repo/relative
                original = target.read_text()
                self.assertIn(before, original)
                target.write_text(original.replace(before, after))
                patch = subprocess.check_output(['git','-C',str(self.repo),'diff'])
                target.write_text(original)
                result = local.execute(self.repo,self.base,patch)
                contract = self.contract | {'allowed_paths':[relative]}
                report = impact.analyze(result,contract,'run-test','sandbox-test')
                self.assertEqual(report['decision'],'block')
                self.assertIn('TRUSTED_TEST_MODIFIED', [finding['code'] for finding in report['findings']])
                self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_dependency_only_package_change_requires_review_and_runs_tests(self):
        source = self.repo/'src/auth/session.ts'
        source.write_text(source.read_text().replace('>= timeoutSeconds;', '>= timeoutSeconds * 1000;'))
        git(self.repo,'add','src/auth/session.ts')
        git(self.repo,'commit','-qm','fixed baseline')
        self.base = git(self.repo,'rev-parse','HEAD')
        self.contract['base_commit'] = self.base
        target = self.repo/'package.json'
        original = target.read_text()
        self.assertIn(',"scripts":', original)
        target.write_text(original.replace(',"scripts":', ',"dependencies":{"left-pad":"1.3.0"},"scripts":'))
        patch = subprocess.check_output(['git','-C',str(self.repo),'diff'])
        target.write_text(original)
        result = local.execute(self.repo,self.base,patch)
        report = impact.analyze(result,self.contract | {'allowed_paths':['package.json']},'run-test','sandbox-test')
        self.assertEqual(result['test_exit'], 0, result['test_output'][-500:].decode(errors='replace'))
        self.assertEqual(report['decision'], 'review_required')
        self.assertEqual([(f['code'],f['severity']) for f in report['findings']], [('DEPENDENCY_CHANGE','medium')])
        self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_stopped_and_tampered_promotion_refused(self):
        patch=self.patch('>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        result=local.execute(self.repo,self.base,patch);report=impact.analyze(result,self.contract,'run-test','sandbox-test')
        artifact=Path(self.temp.name)/'reviewed.patch';artifact.write_bytes(report['patch'])
        with self.assertRaises(promoter.PromotionRefused): promoter.promote(self.repo,artifact,self.base,report['patch_sha256'],lambda:'stopped')
        artifact.write_bytes(report['patch']+b'x')
        with self.assertRaises(promoter.PromotionRefused): promoter.promote(self.repo,artifact,self.base,report['patch_sha256'],lambda:'active')
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
