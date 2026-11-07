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
