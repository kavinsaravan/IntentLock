#!/usr/bin/env bash
# Build the authorizer Lambda zip. Deliberately EXCLUDES the HTTP app and the Bedrock module, so the
# deployed artifact cannot call Bedrock. boto3 is provided by the Lambda runtime. No AWS access needed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/infrastructure/control-plane/build"
rm -rf "$OUT" && mkdir -p "$OUT/pkg/intentlock_api"
python3 -m pip install --quiet --target "$OUT/pkg" --platform manylinux2014_x86_64 \
  --python-version 3.12 --implementation cp --only-binary=:all: --upgrade "pydantic>=2.9,<3"
cp -R "$ROOT/packages/policy-engine/intentlock_policy" "$OUT/pkg/intentlock_policy"
for f in __init__ store dynamo_store service lambda_handler; do cp "$ROOT/apps/api/intentlock_api/$f.py" "$OUT/pkg/intentlock_api/"; done
find "$OUT/pkg" -name '__pycache__' -type d -prune -exec rm -rf {} +
(cd "$OUT/pkg" && zip -q -r -X "$OUT/intentlock-authorizer.zip" .)
echo "built $OUT/intentlock-authorizer.zip ($(du -h "$OUT/intentlock-authorizer.zip" | cut -f1))"
