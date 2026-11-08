import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/mcp-server'))
spec = importlib.util.spec_from_file_location('gateway', Path(__file__).resolve().parents[2] / 'apps/mcp-server/server.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'safe.txt').write_text('hello')
        self.repo = g.Repository(self.root)

    def test_regular_read(self):
        self.assertEqual(self.repo.read('safe.txt'), 'hello')

    def test_reject_paths(self):
        for path in ('../safe.txt', '/etc/passwd', '.env', '.git/config', 'a/../safe.txt', 'a\\b', 'a\x00b'):
            with self.subTest(path=path), self.assertRaises(g.Denied):
                self.repo.read(path)

    def test_symlink(self):
        (self.root / 'link').symlink_to(self.root / 'safe.txt')
        with self.assertRaises(OSError):
            self.repo.read('link')

    def test_intermediate_symlink(self):
        (self.root / 'dir').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.repo.read('dir/safe.txt')

    def test_size_limit(self):
        (self.root / 'big').write_bytes(b'x' * (g.MAX_FILE + 1))
        with self.assertRaises(g.Denied):
            self.repo.read('big')

    def test_deny_never_reads(self):
        repo = Mock()
        server = g.Server(repo, Mock(side_effect=g.Denied('RUN_NOT_ACTIVE')), 'r', 'c', 1)
        with self.assertRaises(g.Denied):
            server.dispatch({'method': 'tools/call', 'params': {'name': 'read_file', 'arguments': {'path': 'safe.txt'}}})
        repo.read.assert_not_called()

    def test_authorization_envelope(self):
        auth = Mock()
        server = g.Server(self.repo, auth, 'r', 'c', 2)
        result = server.dispatch({'method': 'tools/call', 'params': {'name': 'read_file', 'arguments': {'path': 'safe.txt'}}})
        self.assertEqual(result['content'][0]['text'], 'hello')
        self.assertEqual(auth.call_args.args[0]['contract_version'], 2)

    def test_missing_authorizer_fails_closed(self):
        from control_plane import Client, ControlPlaneError
        with self.assertRaises(ControlPlaneError):
            Client('', '')

    def test_listing_and_search_hide_protected_and_disallowed_paths(self):
        (self.root / 'src' / 'auth').mkdir(parents=True)
        (self.root / 'src' / 'auth' / 'session.ts').write_text('const token = 1;\n')
        (self.root / 'src' / 'auth' / '.env.local').write_text('token=secret')
        (self.root / 'src' / 'auth' / 'hidden').mkdir()
        (self.root / 'src' / 'auth' / 'hidden' / 'secret.ts').write_text('token=secret')
        contract = {'contract_id':'c','version':1,'status':'active',
                    'allowed_paths':['src/auth/**'], 'protected_paths':['src/auth/hidden/**']}
        client = Mock()
        client.request.return_value = contract
        server = g.Server(self.repo, Mock(), 'r', 'c', 1)
        with patch.object(g.Client, 'from_env', return_value=client):
            listing = server.dispatch({'method':'tools/call','params':{'name':'list_files','arguments':{'path':'src/auth'}}})
            found = server.dispatch({'method':'tools/call','params':{'name':'search_code','arguments':{'query':'token','paths':['src/auth/**']}}})
        import json
        listed = json.loads(listing['content'][0]['text'])
        matches = json.loads(found['content'][0]['text'])
        self.assertEqual([p['path'] for p in listed], ['src/auth/session.ts'])
        self.assertEqual([p['path'] for p in matches], ['src/auth/session.ts'])

if __name__ == '__main__':
    unittest.main()
