#!/usr/bin/env bash
# READ-ONLY AWS verification for the IntentLock control plane. Creates, changes and deletes NOTHING.
# Never prints credentials; the account id is masked; Bedrock models are only LISTED, never invoked.
#
#   PROFILE=intentlock REGION=us-east-1 EXPECTED_USER=aparpia EXPECTED_ACCOUNT=<12-digit id> \
#     infrastructure/control-plane/verify_aws_readonly.sh
set -u
PROFILE="${PROFILE:-intentlock}"; REGION="${REGION:-us-east-1}"; EXPECTED_USER="${EXPECTED_USER:-aparpia}"
EXPECTED_ACCOUNT="${EXPECTED_ACCOUNT:-}"; STACK="${STACK:-intentlock-control-plane-dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export AWS_PAGER=""
A=(aws --profile "$PROFILE" --region "$REGION" --no-cli-pager)

if ! aws configure list-profiles 2>/dev/null | grep -qx "$PROFILE"; then
  echo "Profile '$PROFILE' does not exist. Create it privately in YOUR terminal (never paste keys into chat):"
  echo "    aws configure --profile $PROFILE      # enter key id, secret, region $REGION, output json"
  exit 2
fi

PASS=0; FAIL=0
res() { # name status detail
  if [ "$2" = ok ]; then PASS=$((PASS+1)); printf '  PASS  %-34s %s\n' "$1" "$3"; else FAIL=$((FAIL+1)); printf '  FAIL  %-34s %s\n' "$1" "$3"; fi
}
err() { sed -n 's/.*(\([A-Za-z]*\)).*/\1/p' | head -1; }
run() { # name, then command...; shows only a short error code on failure
  local name="$1"; shift; local out; out="$("$@" 2>&1)"; local rc=$?
  if [ $rc -eq 0 ]; then res "$name" ok "${RUN_OK:-}"; else res "$name" fail "$(printf '%s' "$out" | err)"; fi
  RUN_OK=""
}

echo "== Identity ($PROFILE, $REGION)"
ID="$("${A[@]}" sts get-caller-identity --output json 2>&1)" || { echo "  FAIL  sts get-caller-identity   $(printf '%s' "$ID" | err)"; exit 1; }
ACCT="$(printf '%s' "$ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Account"])')"
ARN="$(printf '%s' "$ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Arn"])')"
UNAME="${ARN##*/}"; PTYPE="$(printf '%s' "$ARN" | cut -d: -f6 | cut -d/ -f1)"
echo "  account ****${ACCT: -4} | principal type: $PTYPE | IAM username: $UNAME"
[ "$UNAME" = "$EXPECTED_USER" ] && res "username == $EXPECTED_USER" ok "" || res "username == $EXPECTED_USER" fail "got '$UNAME'"
if [ -n "$EXPECTED_ACCOUNT" ]; then [ "$ACCT" = "$EXPECTED_ACCOUNT" ] && res "account == shared account" ok "" || res "account == shared account" fail "differs from EXPECTED_ACCOUNT"
else echo "  NOTE  EXPECTED_ACCOUNT not set: cannot confirm this is the shared hackathon account"; fi
echo "  profile region: $("${A[@]}" configure get region 2>/dev/null || echo unset) (all calls below pass --region $REGION)"

echo "== Read-only service access"
run "dynamodb ListTables"           "${A[@]}" dynamodb list-tables --max-items 5
run "dynamodb DescribeLimits"       "${A[@]}" dynamodb describe-limits
run "lambda GetAccountSettings"     "${A[@]}" lambda get-account-settings
run "lambda ListFunctions"          "${A[@]}" lambda list-functions --max-items 1
run "s3 ListBuckets"                "${A[@]}" s3api list-buckets --query 'length(Buckets)'
run "cloudformation ListStacks"     "${A[@]}" cloudformation list-stacks --max-items 1
run "cloudformation ValidateTemplate" "${A[@]}" cloudformation validate-template --template-body "file://$HERE/template.yaml" --query Capabilities
run "logs DescribeLogGroups"        "${A[@]}" logs describe-log-groups --max-items 1
run "cloudwatch ListMetrics"        "${A[@]}" cloudwatch list-metrics --max-items 1
run "bedrock ListFoundationModels (no invoke)" "${A[@]}" bedrock list-foundation-models --by-provider anthropic --query 'length(modelSummaries)'
if [ -n "${INTENTLOCK_BEDROCK_MODEL_ID:-}" ]; then
  run "bedrock model availability"  "${A[@]}" bedrock get-foundation-model-availability --model-id "$INTENTLOCK_BEDROCK_MODEL_ID"
fi
STK="$("${A[@]}" cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>&1)"
case "$STK" in *"does not exist"*) echo "  stack '$STACK': does not exist (expected before first deploy)";;
               *AccessDenied*) echo "  stack '$STACK': UNKNOWN (AccessDenied: cannot tell whether it exists)";;
               *"An error occurred"*|*ERROR*) echo "  stack '$STACK': UNKNOWN ($(printf '%s' "$STK" | err))";;
               *) echo "  stack '$STACK': EXISTS with status $STK (inspect before deploying)";; esac

echo "== Resource-name conflicts (read-only; each should be 'free' before first deploy)"
name_free() { # label, then a command whose failure message should contain a not-found code
  local out; out="$("${@:2}" 2>&1)" && { res "$1" fail "ALREADY EXISTS"; return; }
  case "$out" in *ResourceNotFound*|*NotFound*|*does\ not\ exist*|*404*) res "$1" ok "free";; *) res "$1" fail "cannot tell: $(printf '%s' "$out" | err)";; esac
}
name_free "table intentlock-control-plane" "${A[@]}" dynamodb describe-table --table-name intentlock-control-plane
name_free "lambda intentlock-authorizer"   "${A[@]}" lambda get-function --function-name intentlock-authorizer
LG="$("${A[@]}" logs describe-log-groups --log-group-name-prefix /aws/lambda/intentlock-authorizer --query 'length(logGroups)' --output text 2>&1)"
if [ "$LG" = "0" ]; then res "log group /aws/lambda/intentlock-authorizer" ok "free"
elif printf '%s' "$LG" | grep -q AccessDenied; then res "log group /aws/lambda/intentlock-authorizer" fail "cannot tell: AccessDenied"
else res "log group /aws/lambda/intentlock-authorizer" fail "EXISTS ($LG)"; fi
if [ -n "${CODE_BUCKET:-}" ]; then
  out="$("${A[@]}" s3api head-bucket --bucket "$CODE_BUCKET" 2>&1)" && res "code bucket $CODE_BUCKET" ok "exists and is accessible" || {
    case "$out" in *404*|*NotFound*) echo "  NOTE  bucket $CODE_BUCKET not found: create it before uploading the Lambda zip (names are global);";;
                   *) res "code bucket $CODE_BUCKET" fail "exists but not accessible, or cannot tell: $(printf '%s' "$out" | err)";; esac; }
else echo "  NOTE  CODE_BUCKET not set: choose an S3 bucket name for the Lambda zip (bucket names are global)"; fi

echo "== Budgets (read-only)"
BUD="$("${A[@]}" budgets describe-budgets --account-id "$ACCT" --query 'length(Budgets)' --output text 2>&1)" && echo "  existing budgets in this account: $BUD" || echo "  cannot read budgets ($(printf '%s' "$BUD" | err))"

if [ "${LIST_MODELS:-0}" = "1" ]; then
  echo "== Bedrock (listing only, nothing invoked): Anthropic foundation models and inference profiles in $REGION"
  "${A[@]}" bedrock list-foundation-models --by-provider anthropic --query 'modelSummaries[].[modelId,modelLifecycle.status]' --output text 2>&1 | head -40
  "${A[@]}" bedrock list-inference-profiles --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `anthropic`)].inferenceProfileId' --output text 2>&1 | tr '\t' '\n' | head -40
fi

echo "== Deploy-time permissions (IAM policy SIMULATION; evaluates your attached policies only, changes nothing)"
ACTIONS=(cloudformation:CreateStack cloudformation:UpdateStack iam:CreateRole iam:PassRole iam:PutRolePolicy iam:AttachRolePolicy
         iam:CreatePolicy dynamodb:CreateTable lambda:CreateFunction lambda:UpdateFunctionCode logs:CreateLogGroup s3:CreateBucket s3:PutObject)
SIM="$("${A[@]}" iam simulate-principal-policy --policy-source-arn "$ARN" --action-names "${ACTIONS[@]}" \
      --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text 2>&1)" && printf '%s\n' "$SIM" | awk '{printf "  %-36s %s\n",$1,$2}' \
  || echo "  could not simulate ($(printf '%s' "$SIM" | err)); these permissions can only be confirmed by deploying"
echo "  (simulation ignores SCPs/permission boundaries and resource policies; the real proof is the deployment itself)"
echo "== Summary: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
