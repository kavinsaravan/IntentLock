# Control-plane infrastructure (minimal; NOT deployed)

Nothing in this repository deploys cloud resources. Before running any command below, confirm with the
owner: **AWS account ID, region, and the target stack name**.

## What the template creates (`template.yaml`)
- DynamoDB table (single table, on-demand, SSE on, point-in-time recovery, `DeletionPolicy: Retain`).
- Authorizer Lambda (`intentlock_api.lambda_handler.handler`, python3.12) + its role and log group.
- `TableAccessPolicy` (GetItem/PutItem/UpdateItem/Query on this table only; no Scan/Delete/admin). The
  Lambda role uses it; attach it to whatever runs the HTTP API against DynamoDB.
- `InvokePolicy` (invoke this one function) to attach to the execution plane's principal.
- Optional IAM-authenticated Function URL (`EnableFunctionUrl=true`).

Not included on purpose: Bedrock permissions (authorization never calls Bedrock), API hosting, Amplify,
AgentCore, S3 evidence bucket (execution plane), CloudWatch alarms/metrics (planned).

## Offline checks (safe; no AWS access)
```bash
pip install cfn-lint && cfn-lint infrastructure/control-plane/template.yaml
infrastructure/control-plane/build_lambda.sh      # builds build/intentlock-authorizer.zip
```

## Deploy (only after confirming account/region/target)
```bash
aws s3 cp infrastructure/control-plane/build/intentlock-authorizer.zip s3://<code-bucket>/intentlock-authorizer.zip
aws cloudformation deploy --region <region> --stack-name <stack> --capabilities CAPABILITY_IAM \
  --template-file infrastructure/control-plane/template.yaml \
  --parameter-overrides CodeBucket=<code-bucket> CodeKey=intentlock-authorizer.zip
```

## Run the HTTP API against the deployed table
```bash
INTENTLOCK_STORE=dynamodb INTENTLOCK_TABLE=<table> AWS_REGION=<region> INTENTLOCK_INTERNAL_TOKEN=<token> \
  python3 -m uvicorn intentlock_api.app:create_app --factory
```

## Verify the real table (opt-in; uses your ambient AWS credentials)
The store contract tests run against a real table when `INTENTLOCK_AWS_TEST_TABLE` is set. They create only
uniquely named items and never delete or scan the table.
```bash
INTENTLOCK_AWS_TEST_TABLE=<table> AWS_REGION=<region> python3 -m pytest apps/api/tests/test_store_contract.py -q
```

## Intended deployment (NOT executed) — region `us-east-1`

- **Stack name:** `intentlock-control-plane-dev`. **Region:** `us-east-1` (confirmed by the owner). **Account:** the owner's (confirm before deploy).
- **Resources created by `template.yaml`:** `AWS::DynamoDB::Table` (`intentlock-control-plane`), `AWS::Logs::LogGroup` (`/aws/lambda/intentlock-authorizer`), `AWS::IAM::ManagedPolicy` x2 (table access, invoke), `AWS::IAM::Role` (authorizer), `AWS::Lambda::Function` (`intentlock-authorizer`), optional `AWS::Lambda::Url` (AWS_IAM). Plus, outside the stack: one S3 bucket/prefix that holds the Lambda zip (created manually or an existing bucket).
- **Permissions the deploying principal needs:** `cloudformation:CreateStack/UpdateStack/DescribeStacks/DescribeStackEvents/GetTemplateSummary`; `iam:CreateRole/PutRolePolicy/AttachRolePolicy/PassRole/CreatePolicy/GetRole/GetPolicy` (scoped to `intentlock-*`); `dynamodb:CreateTable/DescribeTable/UpdateContinuousBackups/TagResource`; `lambda:CreateFunction/UpdateFunctionCode/GetFunction/CreateFunctionUrlConfig`; `logs:CreateLogGroup/PutRetentionPolicy`; `s3:PutObject/GetObject` on the code bucket.
- **Runtime permissions (granted by the template):** the Lambda role may only `GetItem/PutItem/UpdateItem/Query` on that one table and write its own log group. No Scan, no Delete, no Bedrock. The API host (if it uses DynamoDB) needs the same table policy.

### Earlier read-only checks (2026-11-07) — CORRECTION: wrong identity
These ran with the machine's `default` CLI profile, which is IAM user `aparpia@scu.edu` in account `****8656` with profile region `us-west-1`. That is **not** confirmed to be the shared hackathon account or the `aparpia` user, so treat the findings below as about *that* account only and re-run `verify_aws_readonly.sh` with the `intentlock` profile.

### (superseded) read-only checks performed 2026-11-07, `default` profile (no resources created or changed)
`sts get-caller-identity` (credentials valid, an IAM *user* principal), `dynamodb list-tables` (0 tables in us-east-1), `cloudformation describe-stacks intentlock-control-plane-dev` (does not exist), `lambda get-function intentlock-authorizer` (does not exist), `lambda get-account-settings` (**ConcurrentExecutions limit = 10**, so reserved concurrency cannot be used meaningfully), `bedrock list-foundation-models --by-provider anthropic` (15 models listed; listing does NOT prove model access is granted — verify with a real invoke before relying on Bedrock). Nothing else was called.

## Profile setup and read-only verification (do this first)
The `intentlock` profile does not exist yet on this machine. Create it **privately in your own terminal** (never paste keys into chat or a file in this repo):
```bash
aws configure --profile intentlock      # access key id, secret, region us-east-1, output json
```
Then verify (read-only; creates/changes nothing; account id masked; Bedrock only listed, never invoked):
```bash
PROFILE=intentlock REGION=us-east-1 EXPECTED_USER=aparpia EXPECTED_ACCOUNT=<shared 12-digit account id> \
  infrastructure/control-plane/verify_aws_readonly.sh
```
It checks identity/username/account, DynamoDB, Lambda, S3, CloudFormation (including `validate-template` on our template), CloudWatch/Logs, Bedrock model listing, and runs an IAM policy *simulation* for the deploy-time actions. Simulation is evidence, not proof; IAM, CloudFormation, Lambda and S3 write permissions are only truly confirmed by deploying.

## Deployment readiness (assessment, nothing deployed)
- **Stack:** `intentlock-control-plane-dev`, region `us-east-1`. Deploy with `--profile intentlock` and `--capabilities CAPABILITY_IAM` (no custom IAM names, so `CAPABILITY_NAMED_IAM` is not needed).
- **DynamoDB:** 1 table (`intentlock-control-plane`, on-demand, SSE, PITR, Retain). **Lambda:** 1 function (`intentlock-authorizer`, python3.12 x86_64, 256 MB, 10 s). **Log group:** 1. **IAM:** 1 role + 2 managed policies. **S3:** NOT in the template — one bucket is needed to hold the Lambda zip (create it yourself or reuse one); a second bucket for TwinGuard patch/evidence artifacts is Person 2's and no S3 reader/writer exists yet.
- **Environment variables:** Lambda (set by the template): `INTENTLOCK_STORE=dynamodb`, `INTENTLOCK_TABLE`. API host: `INTENTLOCK_INTERNAL_TOKEN`, `INTENTLOCK_STORE`, `INTENTLOCK_TABLE`, `AWS_REGION` (now defaults to `us-east-1` in code), `AWS_PROFILE=intentlock` (local only), `INTENTLOCK_BEDROCK_MODEL_ID`, `INTENTLOCK_REVIEWERS`, `INTENTLOCK_ARTIFACT_DIR` (local stand-in), `INTENTLOCK_PROMOTER_URL`/`INTENTLOCK_PROMOTER_TOKEN`, launcher variables (see `apps/api/.env.example`). Frontend: `VITE_API_URL` / `VITE_API_PROXY` / `VITE_DEMO_BASE_COMMIT`.
- **Not deployable / missing:** the FastAPI app (only the authorizer Lambda is packaged; the API still runs locally), frontend hosting (Amplify), authentication for the public API, S3 artifact reader/writer, CloudWatch custom metrics/alarms/dashboard, AgentCore. Bedrock needs a model id (some models require a cross-region inference-profile id such as `us.<model>`) and granted model access — unverified.
- **Config problems found and fixed:** the DynamoDB store used the ambient profile region (`us-west-1` for the default profile) while Bedrock defaulted to `us-east-1`; both now use one helper that defaults to `us-east-1` (explicit `AWS_REGION` still wins). `/health` shows `dynamodb:<table>@<region>`.
- **Cost (approximate, from published us-east-1 on-demand pricing as I know it; not queried live — confirm on the AWS pricing pages):** DynamoDB on-demand about $1.25 per million write units and $0.25 per million read units; one authorization is roughly 6 reads + 7 writes, so about $0.00001 each (100k authorizations ≈ $1); PITR about $0.20/GB-month; Lambda 1M requests + 400k GB-s/month are free-tier, then $0.20/M requests; CloudWatch Logs about $0.50/GB ingested (30-day retention set); Function URL itself is free. **Main cost risk: Bedrock**, billed per token for the chosen model (each contract proposal is ≤2 calls × ≤1500 output tokens, but a public unauthenticated `/api/contracts/propose` could be spammed). Other risks: DynamoDB on-demand has no spend cap (bounded in practice by the 50-tool-call default per run); the Lambda account concurrency quota seen earlier was 10 (that account only). Recommend an AWS Budget alert before first deploy.
