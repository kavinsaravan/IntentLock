"""Real HTTP integration against an isolated checkout of Person 1's branch."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps/mcp-server'))
from control_plane import Client
from lifecycle import supervise
PERSON1 = os.environ.get('INTENTLOCK_PERSON1_CHECKOUT')

@unittest.skipUnless(PERSON1, 'Set INTENTLOCK_PERSON1_CHECKOUT to an isolated Ali branch checkout')
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        sys.path[:0] = [str(Path(PERSON1) / 'apps/api'), str(Path(PERSON1) / 'packages/policy-engine')]
        from intentlock_api.app import create_app
        cls.sock = socket.socket()
        cls.sock.bind(('127.0.0.1', 0))
        cls.url = 'http://127.0.0.1:' + str(cls.sock.getsockname()[1])
        cls.server = uvicorn.Server(uvicorn.Config(create_app(internal_token='integration-only'), log_level='error'))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets':[cls.sock]}, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError('API did not start')
        cls.client = Client(cls.url, 'integration-only')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)
        cls.sock.close()

    def new_run(self):
        draft = json.loads((Path(PERSON1) / 'packages/contracts/fixtures/contract-draft.json').read_text())
        contract = self.client.request('/api/contracts', {'draft':draft,'approved':True,'approved_by':'integration-test'})
        return self.client.request('/api/runs', {'contract_id':contract['contract_id'],'contract_version':contract['version']})

    def call(self, process, ident, path):
        process.stdin.write(json.dumps({'jsonrpc':'2.0','id':ident,'method':'tools/call','params':{'name':'read_file','arguments':{'path':path}}}) + '\n')
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    def test_stdio_allow_block_replay(self):
        run = self.new_run()
        env = os.environ | {'INTENTLOCK_API_URL':self.url,'INTENTLOCK_INTERNAL_TOKEN':'integration-only'}
        p = subprocess.Popen([sys.executable,'-B',str(ROOT/'apps/mcp-server/server.py'),'--repository',str(ROOT/'demo/repository'),'--run-id',run['run_id'],'--contract-id',run['contract_id'],'--contract-version','1'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertFalse(self.call(p,1,'src/auth/session.ts')['result']['isError'])
            self.assertEqual(self.call(p,2,'.env')['result']['content'][0]['text'],'PROTECTED_PATH')
            self.assertEqual(self.call(p,3,'src/auth/session.ts')['result']['content'][0]['text'],'RUN_NOT_ACTIVE')
            events = self.client.request('/api/runs/'+run['run_id']+'/events')['events']
            reasons = [e.get('reason_code') for e in events if e['event_type']=='tool_request']
            self.assertEqual(reasons,['ALLOW','PROTECTED_PATH','RUN_NOT_ACTIVE'])
            self.assertNotIn('demo-valid-token',json.dumps(events))
        finally:
            p.stdin.close()
            p.wait(timeout=5)
            p.stdout.close()

    def test_stop_terminates_process(self):
        run = self.new_run()
        result = []
        def launch():
            result.append(supervise([sys.executable,'-c','import time; time.sleep(30)'],str(ROOT),self.client,run['run_id'],run['contract_id'],1))
        thread = threading.Thread(target=launch)
        thread.start()
        pid = None
        try:
            for _ in range(100):
                pid = self.client.run(run['run_id']).get('agent_process_id')
                if pid:
                    break
                time.sleep(.05)
            self.assertIsNotNone(pid)
            self.client.request('/api/runs/'+run['run_id']+'/stop',{})
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result,[2])
            self.assertEqual(self.client.run(run['run_id'])['status'],'stopped')
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid),0)
        finally:
            if thread.is_alive():
                self.client.request('/api/runs/'+run['run_id']+'/stop',{})
                thread.join(5)

    def test_supervisor_reports_completion_and_failure(self):
        for exit_code, expected in ((0, 'completed'), (7, 'failed')):
            with self.subTest(exit_code=exit_code):
                run = self.new_run()
                result = supervise([sys.executable, '-c', f'raise SystemExit({exit_code})'],
                                   str(ROOT), self.client, run['run_id'], run['contract_id'], 1)
                self.assertEqual(result, 0 if exit_code == 0 else 2)
                self.assertEqual(self.client.run(run['run_id'])['status'], expected)
                events = self.client.request('/api/runs/'+run['run_id']+'/events')['events']
                self.assertEqual(sum(e['event_type'] == 'run_completed' for e in events), 1)

    def test_sigterm_handler_reports_failure_and_cleans_child(self):
        run = self.new_run()
        with tempfile.TemporaryDirectory() as folder:
            standin = Path(folder) / 'kiro-standin'
            standin.write_text('#!/bin/sh\nsleep 30\n')
            standin.chmod(0o700)
            env = os.environ | {'INTENTLOCK_API_URL':self.url, 'INTENTLOCK_INTERNAL_TOKEN':'integration-only',
                                'KIRO_CLI_BINARY':str(standin)}
            command = [sys.executable, '-B', str(ROOT/'apps/mcp-server/launch_kiro.py'),
                       '--repository',str(ROOT/'demo/repository'), '--run-id',run['run_id'],
                       '--contract-id',run['contract_id'],'--contract-version','1']
            runner = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    child = self.client.run(run['run_id']).get('agent_process_id')
                    if child: break
                    time.sleep(.05)
                else: self.fail('agent PID was never registered')
                os.kill(runner.pid, signal.SIGTERM)
                self.assertEqual(runner.wait(timeout=8), 2)
                self.assertEqual(self.client.run(run['run_id'])['status'], 'failed')
                with self.assertRaises(ProcessLookupError): os.kill(int(child), 0)
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait(5)
