#!/usr/bin/env bash
# Launch the verified localhost demo stack against a fresh disposable Git repository.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
WEB="$ROOT/apps/web"

die() { printf 'Demo preflight failed: %s\n' "$*" >&2; exit 2; }

# Ports are fixed by the runbook; the overrides exist so the port check can be tested
# and so a second stack can run beside an existing one.
API_PORT="${INTENTLOCK_DEMO_API_PORT:-8787}"
PROMOTER_PORT="${INTENTLOCK_DEMO_PROMOTER_PORT:-8790}"
WEB_PORT="${INTENTLOCK_DEMO_WEB_PORT:-5173}"

# Refuse to start if anything already listens on a demo port. Otherwise the new service
# fails to bind, a stale stack answers the readiness probe, and "Demo ready" is false.
port_busy() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
  fi
}
port_owner() {
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {printf " (held by %s, pid %s)", $1, $2}'
}
for spec in "api:$API_PORT" "promoter:$PROMOTER_PORT" "web:$WEB_PORT"; do
  if port_busy "${spec#*:}"; then
    die "port ${spec#*:} (${spec%%:*}) is already in use$(port_owner "${spec#*:}"). Another demo stack may be running; stop it, then retry."
  fi
done

command -v docker >/dev/null || die 'Docker is not installed'
command -v kiro-cli >/dev/null || die 'kiro-cli is not installed'
command -v npm >/dev/null || die 'npm is not installed'
command -v curl >/dev/null || die 'curl is not installed'
[ -x "$PY" ] || die 'Create .venv and install apps/api/requirements-dev.txt'
[ -d "$WEB/node_modules" ] || die 'Run npm ci in apps/web'
docker info >/dev/null 2>&1 || die 'Docker Desktop is not running'
kiro-cli whoami >/dev/null 2>&1 || die 'Run kiro-cli login'
[ -n "${INTENTLOCK_INTERNAL_TOKEN:-}" ] || die 'Set INTENTLOCK_INTERNAL_TOKEN (openssl rand -hex 24)'
[ -n "${INTENTLOCK_PROMOTER_TOKEN:-}" ] || die 'Set INTENTLOCK_PROMOTER_TOKEN (openssl rand -hex 24)'
[ -n "${INTENTLOCK_REVIEWER_TOKENS:-}" ] || die 'Set INTENTLOCK_REVIEWER_TOKENS; see docs/demo-runbook.md'
export INTENTLOCK_STORE=memory
export PORT="$API_PORT"
export INTENTLOCK_API_URL="http://127.0.0.1:$API_PORT"
export INTENTLOCK_PROMOTER_URL="http://127.0.0.1:$PROMOTER_PORT"
export INTENTLOCK_AGENT_ENV_PASSTHROUGH=INTENTLOCK_ARTIFACT_DIR
export INTENTLOCK_CORS_ORIGINS="http://localhost:$WEB_PORT"
"$ROOT/apps/api/run_demo_api.sh" --check >/dev/null || die 'API configuration is invalid'

if [ "${1:-}" = '--check' ]; then
  printf 'Demo preflight OK: Docker, Kiro login, Python, web dependencies, API configuration\n'
  exit 0
fi
[ "${1:-}" = '' ] || die 'Only --check is supported'

DEMO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/intentlock-demo.XXXXXX")"
DEMO_REPO="$DEMO_ROOT/repository"
mkdir -p "$DEMO_REPO" "$DEMO_ROOT/artifacts" "$DEMO_ROOT/logs"
cp -R "$ROOT/demo/repository/." "$DEMO_REPO/"
git -C "$DEMO_REPO" init -q
git -C "$DEMO_REPO" add .
git -C "$DEMO_REPO" -c user.name=IntentLock -c user.email=demo@intentlock.invalid commit -qm baseline
BASE_COMMIT="$(git -C "$DEMO_REPO" rev-parse HEAD)"
export INTENTLOCK_ARTIFACT_DIR="$DEMO_ROOT/artifacts"
export INTENTLOCK_AGENT_LOG_DIR="$DEMO_ROOT/logs/agents"
export INTENTLOCK_AGENT_LAUNCH_ARGV
INTENTLOCK_AGENT_LAUNCH_ARGV="$($PY -c 'import json,sys; print(json.dumps([sys.argv[1], "-B", sys.argv[2], "--repository", sys.argv[3], "--run-id", "{run_id}", "--contract-id", "{contract_id}", "--contract-version", "{contract_version}", "--prompt={mission}"]))' "$PY" "$ROOT/apps/mcp-server/launch_kiro.py" "$DEMO_REPO")"

docker build -q -t intentlock-twinguard:local "$ROOT/packages/sandbox-provider" >/dev/null
"$PY" "$ROOT/packages/patch-promoter/service.py" --repository "$DEMO_REPO" --port "$PROMOTER_PORT" >"$DEMO_ROOT/logs/promoter.log" 2>&1 &
PROMOTER_PID=$!
"$ROOT/apps/api/run_demo_api.sh" >"$DEMO_ROOT/logs/api.log" 2>&1 &
API_PID=$!
(cd "$WEB" && VITE_API_PROXY="http://127.0.0.1:$API_PORT" VITE_DEMO_BASE_COMMIT="$BASE_COMMIT" exec ./node_modules/.bin/vite --host localhost --port "$WEB_PORT" --strictPort) >"$DEMO_ROOT/logs/web.log" 2>&1 &
WEB_PID=$!
cleanup() {
  kill "$WEB_PID" "$API_PID" "$PROMOTER_PID" 2>/dev/null || true
  wait "$WEB_PID" "$API_PID" "$PROMOTER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..50}; do
  kill -0 "$PROMOTER_PID" "$API_PID" "$WEB_PID" 2>/dev/null || die "A demo service exited; see $DEMO_ROOT/logs"
  if curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 &&
     curl -fsS "http://localhost:$WEB_PORT/" >/dev/null 2>&1; then break; fi
  sleep .2
done
kill -0 "$PROMOTER_PID" "$API_PID" "$WEB_PID" 2>/dev/null || die "A demo service exited; see $DEMO_ROOT/logs"
curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null || die "API failed; see $DEMO_ROOT/logs/api.log"
curl -fsS "http://localhost:$WEB_PORT/" >/dev/null || die "Web app failed; see $DEMO_ROOT/logs/web.log"
printf 'Demo ready: http://localhost:%s/\nBase commit: %s\nDemo files and private logs: %s\nReviewer: demo-user (use the token you set in INTENTLOCK_REVIEWER_TOKENS)\n' "$WEB_PORT" "$BASE_COMMIT" "$DEMO_ROOT"
printf 'Keep this terminal open. Stop any active run in the dashboard before pressing Ctrl-C.\n'
wait "$WEB_PID"
