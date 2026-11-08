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
[ ! -f package.json ] || cp package.json /out/package-before.json
if ! git apply --check --recount /patch/input.patch >/dev/null 2>&1; then
  touch /out/patch-invalid
  exit 2
fi
git apply --recount /patch/input.patch
[ ! -f package.json ] || cp package.json /out/package-after.json
git add -N .
git diff --binary > /out/result.patch
git diff --numstat > /out/numstat.txt
git diff --name-status > /out/names.txt
# A changed test file/config or package script must never control the fixed test command.
trusted_change=$(git diff --name-only | grep -E '(^|/)(tests?|__tests__)/|\.(test|spec)\.|(^|/)(jest\.config\.|vitest\.config\.)' || true)
package_safe=1
if git diff --name-only | grep -Fx package.json >/dev/null; then
  node -e '
    const fs = require("fs");
    try {
      const before = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      const after = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
      const strip = value => {
        if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("invalid package");
        for (const key of ["dependencies", "devDependencies", "optionalDependencies", "peerDependencies"]) delete value[key];
        return JSON.stringify(value);
      };
      if (strip(before) !== strip(after)) process.exit(1);
    } catch (_) { process.exit(1); }
  ' /out/package-before.json /out/package-after.json || package_safe=0
fi
if [ -f /out/run-test ] && [ -z "$trusted_change" ] && [ "$package_safe" -eq 1 ]; then
  set +e
  (ulimit -f 2048; npm run test:auth) > /out/test-output.txt 2>&1
  echo "$?" > /out/test-exit.txt
fi
