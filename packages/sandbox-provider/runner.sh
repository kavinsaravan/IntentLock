#!/bin/sh
set -eu
mkdir /work/repo
cp -a /input/. /work/repo/
cd /work/repo
git init -q
git config user.email sandbox@intentlock.invalid
git config user.name TwinGuard
git add .
git commit -qm baseline
git apply --check /patch/input.patch
git apply /patch/input.patch
git add -N .
git diff --binary > /out/result.patch
git diff --numstat > /out/numstat.txt
git diff --name-status > /out/names.txt
# The test catalogue is fixed by the trusted caller, never by a model-provided string.
if [ -f /out/run-test ] && ! git diff --name-only | grep -E '(^|/)(tests?|__tests__)/|\.(test|spec)\.|(^|/)(package\.json|jest\.config\.|vitest\.config\.)' >/dev/null; then
  set +e
  (ulimit -f 2048; npm run test:auth) > /out/test-output.txt 2>&1
  echo "$?" > /out/test-exit.txt
fi
