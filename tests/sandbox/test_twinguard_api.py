"""Actual HTTP control plane, Docker TwinGuard and HTTP promoter, using local artifacts."""
import json
import os
import re
import shutil
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
P1 = os.environ.get('INTENTLOCK_PERSON1_CHECKOUT')
sys.path.insert(0, str(ROOT / 'apps/mcp-server'))
sys.path.insert(0, str(ROOT / 'packages/patch-promoter'))
sys.path.insert(0, str(ROOT / 'packages/sandbox-provider'))
from control_plane import Client
from twinguard import preview
import service as promoter_service
from export_audit import export as export_audit


@unittest.skipUnless(P1, 'Set INTENTLOCK_PERSON1_CHECKOUT')
class TwinGuardApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        sys.path[:0] = [str(Path(P1)/'apps/api'), str(Path(P1)/'packages/policy-engine')]
        from intentlock_api.app import create_app
        from intentlock_api.promoter import PromoterClient
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.repo = cls.root / 'repo'
        shutil.copytree(ROOT / 'demo/repository', cls.repo)
        subprocess.run(['git', '-C', str(cls.repo), 'init', '-q'], check=True)
        for args in (('config','user.email','demo@intentlock.invalid'),('config','user.name','IntentLock'),('add','.'),('commit','-qm','baseline')):
            subprocess.run(['git','-C',str(cls.repo),*args],check=True)
        cls.base = subprocess.check_output(['git','-C',str(cls.repo),'rev-parse','HEAD']).decode().strip()
        cls.artifacts = cls.root / 'artifacts'
        cls.artifacts.mkdir()
        cls.previous_artifact_dir = os.environ.get('INTENTLOCK_ARTIFACT_DIR')
        os.environ['INTENTLOCK_ARTIFACT_DIR'] = str(cls.artifacts)
        cls.artifact_store = promoter_service.artifacts_module.from_env()
        cls.api_socket = socket.socket()
        cls.api_socket.bind(('127.0.0.1',0))
        cls.api_url = 'http://127.0.0.1:' + str(cls.api_socket.getsockname()[1])
        cls.token = 'local-internal-test-token'
        cls.reviewer_token = 'local-reviewer-test-token-long'
        cls.promoter_token = 'local-promoter-test-token-long'
        verifier = promoter_service.Verifier(cls.repo, cls.artifact_store, cls.api_url, cls.token)
        cls.promoter = ThreadingHTTPServer(('127.0.0.1',0), promoter_service.handler(verifier, cls.promoter_token))
        cls.promoter_url = 'http://127.0.0.1:' + str(cls.promoter.server_address[1])
        cls.promoter_thread = threading.Thread(target=cls.promoter.serve_forever,daemon=True)
        cls.promoter_thread.start()
        app = create_app(internal_token=cls.token, artifacts=cls.artifact_store,
                         promoter=PromoterClient(cls.promoter_url,cls.promoter_token), reviewer_tokens={'reviewer':cls.reviewer_token})
        cls.api = uvicorn.Server(uvicorn.Config(app,log_level='error'))
        cls.api_thread = threading.Thread(target=cls.api.run,kwargs={'sockets':[cls.api_socket]},daemon=True)
        cls.api_thread.start()
        for _ in range(100):
            if cls.api.started: break
            time.sleep(.05)
        else: raise RuntimeError('API did not start')
        cls.client = Client(cls.api_url,cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.api.should_exit = True
        cls.api_thread.join(5)
        cls.promoter.shutdown()
        cls.promoter_thread.join(5)
        cls.promoter.server_close()
        cls.api_socket.close()
        if cls.previous_artifact_dir is None:
            os.environ.pop('INTENTLOCK_ARTIFACT_DIR', None)
        else:
            os.environ['INTENTLOCK_ARTIFACT_DIR'] = cls.previous_artifact_dir
        cls.temp.cleanup()

    def run_contract(self):
        draft = json.loads((Path(P1)/'packages/contracts/fixtures/contract-draft.json').read_text())
        draft['base_commit'] = self.base
        draft['allowed_paths'] = ['src/auth/**']
        contract = self.client.request('/api/contracts',{'draft':draft,'approved':True,'approved_by':'reviewer'})
        run = self.client.request('/api/runs',{'contract_id':contract['contract_id'],'contract_version':contract['version']})
        return contract,run

    def patch(self,old,new):
        source=self.repo/'src/auth/session.ts'
        original=source.read_text()
        source.write_text(original.replace(old,new))
        patch=subprocess.check_output(['git','-C',str(self.repo),'diff']).decode()
        source.write_text(original)
        return patch

    def post(self,path,body):
        req=urllib.request.Request(self.api_url+path, data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json','X-IntentLock-Reviewer-Token':self.reviewer_token})
        with urllib.request.urlopen(req,timeout=10) as response: return json.load(response)

    def test_blocked_then_approved_promotion(self):
        contract,run=self.run_contract()
        unsafe=self.patch("token === 'demo-valid-token'",'true')
        bad=preview(self.client,self.repo,run['run_id'],contract['contract_id'],contract['version'],unsafe)
        self.assertEqual(bad['decision'],'block')
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            self.post('/api/impact/'+bad['impact_report_id']+'/approve',{'reviewer':'reviewer','patch_sha256':bad['patch_sha256'],'base_commit':self.base})
        self.assertEqual(blocked.exception.code,409)
        blocked.exception.close()
        self.assertEqual(subprocess.check_output(['git','-C',str(self.repo),'status','--porcelain']),b'')
        safe=self.patch('>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        stopped=preview(self.client,self.repo,run['run_id'],contract['contract_id'],contract['version'],safe)
        stopped_path='/api/impact/'+stopped['impact_report_id']
        self.post(stopped_path+'/approve',{'reviewer':'reviewer','patch_sha256':stopped['patch_sha256'],'base_commit':self.base})
        self.client.request('/api/runs/'+run['run_id']+'/stop',{})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.post(stopped_path+'/promote',{'requested_by':'reviewer','patch_sha256':stopped['patch_sha256']})
        self.assertEqual(refused.exception.code,409)
        refused.exception.close()
        run=self.client.request('/api/runs',{'contract_id':contract['contract_id'],'contract_version':contract['version']})
        good=preview(self.client,self.repo,run['run_id'],contract['contract_id'],contract['version'],safe)
        self.assertEqual(good['decision'],'pass')
        path='/api/impact/'+good['impact_report_id']
        self.post(path+'/approve',{'reviewer':'reviewer','patch_sha256':good['patch_sha256'],'base_commit':self.base})
        result=self.post(path+'/promote',{'requested_by':'reviewer','patch_sha256':good['patch_sha256']})
        self.assertEqual(result['promotion']['status'],'promoted',result)
        self.assertIn('timeoutSeconds * 1000',(self.repo/'src/auth/session.ts').read_text())
        self.client.request('/internal/runs/'+run['run_id']+'/state', {'status':'completed'})
        audit_key,audit_digest=export_audit(self.client,self.artifact_store,run['run_id'])
        self.assertEqual(audit_key,f"runs/{run['run_id']}/audit/final.json")
        self.assertEqual(len(audit_digest),64)

    @unittest.skipUnless(os.environ.get('INTENTLOCK_REAL_KIRO') == '1', 'Explicit real Kiro opt-in')
    def test_real_kiro_proposes_reviewed_patch(self):
        if not shutil.which('kiro-cli'):
            self.skipTest('kiro-cli is not installed')
        contract,run=self.run_contract()
        safe_patch=self.patch('>= timeoutSeconds;', '>= timeoutSeconds * 1000;')
        prompt = (
            'Use only the IntentLock read_file and propose_patch tools. '
            'First read src/auth/session.ts. Then call propose_patch once with the exact '
            'unified Git diff below as the patch string. Copy only the diff bytes, without '
            'Markdown fences or extra explanation. Stop after you receive the impact report ID.\n'
            + safe_patch
        )
        if os.environ.get('INTENTLOCK_KIRO_GENERATE_PATCH') == '1':
            prompt = (
                'Use only the IntentLock read_file and propose_patch tools. Read src/auth/session.ts. '
                'Fix the timeout bug by changing the comparison to '
                'now - createdAt >= timeoutSeconds * 1000. Generate a valid unified Git diff for '
                'src/auth/session.ts and submit it with propose_patch. Stop after receiving its impact report ID.'
            )
        command=[sys.executable,'-B',str(ROOT/'apps/mcp-server/launch_kiro.py'),
                 '--repository',str(self.repo),'--run-id',run['run_id'],
                 '--contract-id',contract['contract_id'],'--contract-version',str(contract['version']),
                 '--prompt',prompt]
        env=os.environ | {'INTENTLOCK_API_URL':self.api_url,'INTENTLOCK_INTERNAL_TOKEN':self.token,
                          'KIRO_CLI_BINARY':shutil.which('kiro-cli')}
        runner=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            output,_=runner.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            self.client.request('/api/runs/'+run['run_id']+'/stop',{})
            runner.communicate(timeout=10)
            self.fail('real Kiro did not finish within 180 seconds')
        events=self.client.request('/api/runs/'+run['run_id']+'/events')['events']
        tools=[e for e in events if e['event_type']=='tool_request']
        impacts=[e['details']['impact_report_id'] for e in events if e['event_type']=='impact_report'
                 and isinstance(e.get('details'),dict) and e['details'].get('impact_report_id')]
        self.assertIn('read_file',[e['tool'] for e in tools])
        self.assertIn('propose_patch',[e['tool'] for e in tools])
        self.assertTrue(impacts, {'exit_code':runner.returncode,'tools':[e['tool'] for e in tools],
                                  'preview_errors':re.findall(r'IMPACT_BLOCKED:[A-Za-z0-9_]+',output)})
        report=self.client.request('/api/impact/'+impacts[-1])
        self.assertEqual(report['decision'],'pass', [f['code'] for f in report['findings']])
        self.assertEqual(self.client.run(run['run_id'])['status'],'completed')
        self.assertEqual(subprocess.check_output(['git','-C',str(self.repo),'status','--porcelain']),b'')
        path='/api/impact/'+report['impact_report_id']
        self.post(path+'/approve',{'reviewer':'reviewer','patch_sha256':report['patch_sha256'],'base_commit':self.base})
        promotion=self.post(path+'/promote',{'requested_by':'reviewer','patch_sha256':report['patch_sha256']})
        self.assertEqual(promotion['promotion']['status'],'promoted',promotion)
        self.assertIn('timeoutSeconds * 1000',(self.repo/'src/auth/session.ts').read_text())
