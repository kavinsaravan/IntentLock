"""Local integration adapter: run Ali's API with the Person 2 S3 reader."""
import argparse
import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('intentlock_artifacts', ROOT/'packages/sandbox-provider/artifacts.py')
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--person1-checkout', required=True)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    if not os.environ.get('INTENTLOCK_ARTIFACT_BUCKET'):
        raise SystemExit('INTENTLOCK_ARTIFACT_BUCKET is required')
    p1 = Path(args.person1_checkout).resolve(strict=True)
    sys.path[:0] = [str(p1/'apps/api'), str(p1/'packages/policy-engine')]
    from intentlock_api.app import create_app
    import uvicorn
    uvicorn.run(create_app(artifacts=artifacts.from_env()), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
