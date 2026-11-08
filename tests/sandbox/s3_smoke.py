"""Explicit opt-in real S3 artifact round trip (one stable synthetic object)."""
import hashlib
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('intentlock_artifacts', ROOT/'packages/sandbox-provider/artifacts.py')
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def main():
    if not os.environ.get('INTENTLOCK_ARTIFACT_BUCKET'):
        raise SystemExit('set INTENTLOCK_ARTIFACT_BUCKET explicitly')
    store = artifacts.from_env()
    key = 'runs/s3_smoke/tests/roundtrip.txt'
    data = b'IntentLock synthetic S3 artifact round trip v1\n'
    digest = hashlib.sha256(data).hexdigest()
    assert store.put(key, data) == digest
    assert store.read(key) == data
    print(f'bucket={store.bucket} key={key} sha256={digest} verified')


if __name__ == '__main__':
    main()
