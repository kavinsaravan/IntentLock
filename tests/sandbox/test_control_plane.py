import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/mcp-server'))
from control_plane import Client, ControlPlaneError, Denied

class Decisions(unittest.TestCase):
    def setUp(self):
        self.client = Client('http://127.0.0.1:8787', 'test-token')
        self.request = {'request_id':'r', 'run_id':'run', 'contract_id':'contract', 'contract_version':1, 'tool':'read_file', 'arguments':{'path':'src/auth/session.ts'}, 'agent_id':'kiro-cli', 'timestamp':'2026-11-07T12:00:00Z'}
        self.decision = {'request_id':'r','decision':'allow','reason_code':'ALLOW','message':'Allowed','latency_ms':1,'stop_run':False}

    def test_allow(self):
        self.client.request = Mock(return_value=self.decision)
        self.client.authorize(self.request)

    def test_malformed_and_inconsistent_denied(self):
        for delta in ({'request_id':'other'}, {'reason_code':'PROTECTED_PATH'}, {'latency_ms':-1}, {'stop_run':'false'}, {'unknown':True}):
            with self.subTest(delta=delta):
                self.client.request = Mock(return_value=self.decision | delta)
                with self.assertRaises(Denied):
                    self.client.authorize(self.request)

    def test_reason_preserved(self):
        self.client.request = Mock(return_value=self.decision | {'decision':'deny','reason_code':'RUN_NOT_ACTIVE'})
        with self.assertRaises(Denied) as result:
            self.client.authorize(self.request)
        self.assertEqual(str(result.exception), 'RUN_NOT_ACTIVE')
        self.assertTrue(result.exception.stop_run)

    def test_unavailable(self):
        self.client.request = Mock(side_effect=ControlPlaneError())
        with self.assertRaises(Denied):
            self.client.authorize(self.request)

    def test_transport_configuration(self):
        for url in ('http://example.com', 'https://user:password@example.com', 'file:///tmp/test'):
            with self.assertRaises(ControlPlaneError):
                Client(url, 'secret')
