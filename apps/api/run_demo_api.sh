#!/usr/bin/env bash
# Start the IntentLock control-plane API for the live demo with SECURE DEFAULTS. Refuses to start on unsafe config.
#
#   export INTENTLOCK_INTERNAL_TOKEN=$(openssl rand -hex 24)                      # shared with Person 2's MCP server
#   export INTENTLOCK_REVIEWER_TOKENS="{\"demo-user\":\"$(openssl rand -hex 24)\"}"  # per-reviewer secrets (>=16 chars, unique)
#   INTENTLOCK_STORE=dynamodb AWS_PROFILE=intentlock apps/api/run_demo_api.sh      # or INTENTLOCK_STORE=memory (default)
#   apps/api/run_demo_api.sh --check                                               # validate config only, start nothing
#
# Optional: PORT (8787), INTENTLOCK_ARTIFACT_DIR, INTENTLOCK_PROMOTER_URL/TOKEN, INTENTLOCK_AGENT_LAUNCH_ARGV (see .env.example).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)}"
PORT="${PORT:-8787}"; HOST="${HOST:-127.0.0.1}"; STORE="${INTENTLOCK_STORE:-memory}"
die() { echo "REFUSING TO START: $*" >&2; exit 2; }

# 1. Network exposure: the public API has no user authentication, so it stays on this machine.
case "$HOST" in 127.0.0.1|localhost|::1) ;; *) [ "${ALLOW_NON_LOCAL:-0}" = "1" ] || die "HOST=$HOST is not loopback. The public API is unauthenticated; bind to 127.0.0.1 (set ALLOW_NON_LOCAL=1 only behind a real authenticating proxy)." ;; esac

# 2. Shared internal token (Person 2's MCP server -> /internal/*)
T="${INTENTLOCK_INTERNAL_TOKEN:-}"
[ -n "$T" ] || die "INTENTLOCK_INTERNAL_TOKEN is not set (the internal API would be disabled)."
[ "${#T}" -ge 24 ] || die "INTENTLOCK_INTERNAL_TOKEN is too short (need >= 24 chars; use: openssl rand -hex 24)."
case "$(printf '%s' "$T" | tr 'A-Z' 'a-z')" in *change-me*|*replace-with*|*dev-token*|*example*|*password*) die "INTENTLOCK_INTERNAL_TOKEN looks like a placeholder.";; esac

# 3. Reviewer tokens: reuse the application's own validator (>=16 chars, no placeholders, unique per reviewer)
[ -n "${INTENTLOCK_REVIEWER_TOKENS:-}" ] || die "INTENTLOCK_REVIEWER_TOKENS is not set (approve/reject/promote would fail closed)."
PYTHONPATH="$ROOT/packages/policy-engine:$HERE" "$PY" -c "from intentlock_api.impact import reviewer_tokens_from_env as f; t=f(); assert t, 'empty token map'; print('reviewers configured:', ', '.join(sorted(t)))" || die "INTENTLOCK_REVIEWER_TOKENS is invalid (see message above)."

# 4. Store / AWS
export AWS_REGION="${AWS_REGION:-us-east-1}"
if [ "$STORE" = "dynamodb" ]; then
  [ "$AWS_REGION" = "us-east-1" ] || die "AWS_REGION=$AWS_REGION; this deployment is us-east-1 only."
  [ -n "${INTENTLOCK_TABLE:-}" ] || export INTENTLOCK_TABLE="intentlock-control-plane"
  P="${AWS_PROFILE:-}"
  [ -n "$P" ] || die "set AWS_PROFILE (e.g. intentlock). Ambient/default credentials are never used for IntentLock."
  [ "$P" != "default" ] || die "AWS_PROFILE=default is not allowed for IntentLock."
  if [ "${SKIP_AWS_CHECK:-0}" != "1" ]; then
    ACCT="$(aws --profile "$P" --region "$AWS_REGION" --no-cli-pager sts get-caller-identity --query Account --output text 2>/dev/null)" || die "profile '$P' cannot authenticate to AWS."
    [ "$ACCT" = "247495661854" ] || die "profile '$P' is account ****${ACCT: -4}, expected the shared account 247495661854."
    echo "AWS identity OK: shared account, profile $P, region $AWS_REGION, table $INTENTLOCK_TABLE"
    echo "NOTE: prefer an identity limited to the stack's TableAccessPolicy (GetItem/PutItem/UpdateItem/Query on the one table); AdministratorAccess works but is far broader than this process needs."
  fi
elif [ "$STORE" = "memory" ]; then
  echo "STORE: in-memory LOCAL FALLBACK (nothing persisted, NOT DynamoDB)"
else
  die "INTENTLOCK_STORE must be 'memory' or 'dynamodb' (got '$STORE')."
fi

export INTENTLOCK_CORS_ORIGINS="${INTENTLOCK_CORS_ORIGINS:-http://localhost:5173}"
echo "listening on http://$HOST:$PORT (CORS: $INTENTLOCK_CORS_ORIGINS)"
[ "${1:-}" = "--check" ] && { echo "config OK (--check: not starting)"; exit 0; }
cd "$HERE" && PYTHONPATH="$ROOT/packages/policy-engine:$HERE" exec "$PY" -m uvicorn intentlock_api.app:create_app --factory --host "$HOST" --port "$PORT"
