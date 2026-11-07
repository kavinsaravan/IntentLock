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

### Read-only checks performed 2026-11-07 (no resources created or changed)
`sts get-caller-identity` (credentials valid, an IAM *user* principal), `dynamodb list-tables` (0 tables in us-east-1), `cloudformation describe-stacks intentlock-control-plane-dev` (does not exist), `lambda get-function intentlock-authorizer` (does not exist), `lambda get-account-settings` (**ConcurrentExecutions limit = 10**, so reserved concurrency cannot be used meaningfully), `bedrock list-foundation-models --by-provider anthropic` (15 models listed; listing does NOT prove model access is granted — verify with a real invoke before relying on Bedrock). Nothing else was called.
