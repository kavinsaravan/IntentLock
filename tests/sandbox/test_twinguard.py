"""Real Docker isolation and exact-patch behavior on a disposable demo Git repo."""
import importlib.util
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

    def test_stopped_and_tampered_promotion_refused(self):
        patch=self.patch('>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        result=local.execute(self.repo,self.base,patch);report=impact.analyze(result,self.contract,'run-test','sandbox-test')
        artifact=Path(self.temp.name)/'reviewed.patch';artifact.write_bytes(report['patch'])
        with self.assertRaises(promoter.PromotionRefused): promoter.promote(self.repo,artifact,self.base,report['patch_sha256'],lambda:'stopped')
        artifact.write_bytes(report['patch']+b'x')
        with self.assertRaises(promoter.PromotionRefused): promoter.promote(self.repo,artifact,self.base,report['patch_sha256'],lambda:'active')
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
