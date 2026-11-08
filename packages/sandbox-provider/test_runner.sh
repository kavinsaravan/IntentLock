#!/bin/sh
set -eu
cp -R /input/. /work/
cd /work
set +e
npm run test:auth > /out/test-output.txt 2>&1
code=$?
set -e
printf '%s' "$code" > /out/test-exit.txt
