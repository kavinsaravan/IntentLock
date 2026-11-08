#!/bin/sh
set -eu
cp -R /input/. /work/
cd /work
set +e
if [ -f /check/checks.txt ]; then
  # One trusted catalogue command, written by the host: "<id> <timeout_seconds> <argv...>". Never agent-supplied.
  read -r _id secs argv < /check/checks.txt
  set -f
  (ulimit -f 2048; timeout "$secs" $argv) > /out/test-output.txt 2>&1
  code=$?
  set +f
else
  npm run test:auth > /out/test-output.txt 2>&1
  code=$?
fi
set -e
printf '%s' "$code" > /out/test-exit.txt
