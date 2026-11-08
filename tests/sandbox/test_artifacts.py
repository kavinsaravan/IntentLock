"""Artifact immutability, key confinement, size and hash checks."""
import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('intentlock_artifacts', ROOT/'packages/sandbox-provider/artifacts.py')
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)
audit_spec = importlib.util.spec_from_file_location('intentlock_audit', ROOT/'packages/sandbox-provider/export_audit.py')
audit = importlib.util.module_from_spec(audit_spec)
audit_spec.loader.exec_module(audit)


class PreconditionFailed(Exception):
    response = {'Error': {'Code': 'PreconditionFailed'}}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **args):
        key = args['Key']
        if key in self.objects:
            raise PreconditionFailed()
        self.objects[key] = {'Body': args['Body'], 'Metadata': args['Metadata'],
                             'ChecksumSHA256': args['ChecksumSHA256'], 'SSE': args['ServerSideEncryption']}

    def get_object(self, **args):
        item = self.objects[args['Key']]
        return {'Body': BytesIO(item['Body']), 'ContentLength': len(item['Body']),
                'Metadata': item['Metadata'], 'ChecksumSHA256': item['ChecksumSHA256']}


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.key = 'runs/run_01/patches/' + hashlib.sha256(b'patch').hexdigest() + '.patch'

    def test_local_is_immutable_and_confined(self):
        with tempfile.TemporaryDirectory() as root:
            store = artifacts.LocalArtifacts(root)
            digest = store.put(self.key, b'patch')
            self.assertEqual(digest, hashlib.sha256(b'patch').hexdigest())
            self.assertEqual(store.put(self.key, b'patch'), digest)
            self.assertEqual((Path(root)/self.key).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(artifacts.ArtifactError): store.put(self.key, b'changed')
            for key in ('../outside', 'runs/run_01/../../secret', 'runs/run_01/patches/not-a-hash.patch'):
                with self.assertRaises(artifacts.ArtifactError): store.read(key)
            self.assertEqual(store.read(self.key), b'patch')

    def test_s3_conditional_write_and_read_hash(self):
        fake = FakeS3()
        store = artifacts.S3Artifacts('intentlock-evidence-example', client=fake)
        digest = store.put(self.key, b'patch')
        self.assertEqual(fake.objects[self.key]['SSE'], 'AES256')
        self.assertEqual(store.put(self.key, b'patch'), digest)
        with self.assertRaisesRegex(artifacts.ArtifactError, 'ARTIFACT_CONFLICT'):
            store.put(self.key, b'changed')
        fake.objects[self.key]['Body'] = b'tampered'
        with self.assertRaisesRegex(artifacts.ArtifactError, 'ARTIFACT_HASH_MISMATCH'):
            store.read(self.key)

    def test_s3_size_bound(self):
        fake = FakeS3()
        store = artifacts.S3Artifacts('intentlock-evidence-example', client=fake)
        with self.assertRaisesRegex(artifacts.ArtifactError, 'INVALID_ARTIFACT_SIZE'):
            store.put(self.key, b'x' * (artifacts.MAX_OBJECT_BYTES + 1))
        archive='runs/run_01/input/repository.tar.gz'
        self.assertEqual(store.put(archive, b'x' * (artifacts.MAX_OBJECT_BYTES + 1)),
                         hashlib.sha256(b'x' * (artifacts.MAX_OBJECT_BYTES + 1)).hexdigest())

    def test_environment_selects_s3_or_local(self):
        fake = FakeS3()
        with tempfile.TemporaryDirectory() as root:
            self.assertIsInstance(artifacts.from_env({'INTENTLOCK_ARTIFACT_DIR': root}), artifacts.LocalArtifacts)
            self.assertIsInstance(artifacts.from_env({'INTENTLOCK_ARTIFACT_DIR': root,
                                                      'INTENTLOCK_ARTIFACT_BUCKET': 'intentlock-evidence-example'}, client=fake), artifacts.S3Artifacts)

    def test_final_audit_requires_terminal_run_and_is_immutable(self):
        class Client:
            status = 'completed'
            def run(self, run_id):
                return {'run_id': run_id, 'status': self.status, 'contract_id': 'contract_01', 'contract_version': 1}
            def request(self, path):
                if path.endswith('/events'):
                    return {'events': [{'event_type': 'impact_report', 'details': {'impact_report_id': 'impact_01'}}]}
                if path.endswith('/status'):
                    return {'review': {'reviewer': 'human'}, 'promotion': {'status': 'promoted'}}
                return {'id': path}
        with tempfile.TemporaryDirectory() as root:
            store = artifacts.LocalArtifacts(root)
            client = Client()
            client.status = 'active'
            with self.assertRaisesRegex(ValueError, 'RUN_NOT_TERMINAL'):
                audit.export(client, store, 'run_01')
            client.status = 'completed'
            key, digest = audit.export(client, store, 'run_01')
            self.assertEqual(key, 'runs/run_01/audit/final.json')
            self.assertEqual(digest, hashlib.sha256(store.read(key)).hexdigest())
            self.assertEqual(audit.export(client, store, 'run_01'), (key, digest))

    def test_final_audit_waits_for_human_promotion(self):
        class Client:
            def run(self, run_id):
                return {'run_id': run_id, 'status': 'completed', 'contract_id': 'contract_01', 'contract_version': 1}
            def request(self, path):
                if path.endswith('/events'):
                    return {'events': [{'event_type': 'impact_report', 'details': {'impact_report_id': 'impact_01'}}]}
                if path.endswith('/status'):
                    return {'review': None, 'promotion': None}
                if path.startswith('/api/impact/'):
                    return {'decision': 'pass'}
                return {'id': path}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'RUN_REVIEW_PENDING'):
                audit.export(Client(), artifacts.LocalArtifacts(root), 'run_01')
            self.assertFalse((Path(root) / 'runs/run_01/audit/final.json').exists())
