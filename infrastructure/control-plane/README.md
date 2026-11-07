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

## Team configuration (confirmed by the owner) and review notes
- Account `247495661854`, IAM user `aparpia`, region `us-east-1`, CLI profile `intentlock`, stack `intentlock-control-plane-dev`. The unrelated `default` profile is never used for IntentLock. **Never reuse the previously exposed access key** (Kavin must deactivate it first); configure the profile with the replacement key, entered privately: `aws configure --profile intentlock` (region `us-east-1`, output `json`).
- Extra read-only checks in `verify_aws_readonly.sh`: resource-name conflicts (table, function, log group, optional `CODE_BUCKET`), existing budgets, and `LIST_MODELS=1` to list Anthropic model/inference-profile ids so a Bedrock model can be chosen (nothing is ever invoked). `INTENTLOCK_BEDROCK_MODEL_ID` additionally runs `get-foundation-model-availability`.
- **Bedrock:** no model id is chosen yet. Anthropic models on Bedrock may need a one-time account-level use-case/subscription step and sometimes a cross-region inference-profile id (`us.…`); this cannot be confirmed without the account. Model *listing* is not proof of access.
- **Budget recommendation (not created):** one account budget of **$25** total for the hackathon window with email alerts at 40% (~$10) actual, 80% (~$20) actual and 100% forecasted. Expected DynamoDB/Lambda/log cost for the whole event is cents; Bedrock tokens are the only realistic overrun. Creating it needs `budgets:CreateBudget` (needs the owner's approval) e.g. `aws budgets create-budget --account-id 247495661854 --profile intentlock --budget file://budget.json --notifications-with-subscribers file://notifications.json`.
- **Least-privilege review of `template.yaml`:** the Lambda role has only `GetItem/PutItem/UpdateItem/Query` on the one table ARN and `logs:CreateLogStream/PutLogEvents` on its own log group; no `Scan`, no delete, no wildcard resources, no Bedrock, no S3. DynamoDB transactions need only the underlying Put/Update permissions (per AWS docs). Invoking is a separate managed policy limited to the one function. Caveats: moto does not enforce IAM, so these policies are unproven until a real deployment exercises them; the managed `TableAccessPolicy` is reusable by the API host and grants it the same table-wide write.
- **Public API surface that needs authentication/rate limiting before exposure beyond localhost:** `POST /api/contracts/propose` (Bedrock cost), `POST /api/runs/{id}/launch` (spawns a process), `POST /api/impact/{id}/approve|reject|promote` (security-relevant; reviewer identity is self-asserted), `POST /api/contracts`, `POST /api/runs`, `POST /api/runs/{id}/stop`, and the read endpoints for events (SSE holds connections), patches and impact reports. Until authentication exists keep the API on `127.0.0.1` (the Lambda authorizer is IAM-protected and is not affected).
- **Logging:** only decision metadata (request id, run id, decision, reason code, latency) and exception class names are logged; events store redacted arguments only. Found and fixed: the Lambda runtime would have dropped the INFO audit lines, so `intentlock.*` loggers are now set to INFO in the handler.
- **Dependencies on Person 2's infrastructure:** none in the template. They need an S3 bucket plus writer for patch/evidence artifacts (the control plane only has a local-directory reader), and a promoter service reachable at `INTENTLOCK_PROMOTER_URL`. They use HTTP, so they do not need the Lambda invoke policy.

## ONE shared AWS account — ownership and naming (PROPOSED to Person 2; not yet agreed)
Account `247495661854` (owned by Kavin), region `us-east-1`, a single environment. Everything for IntentLock is deployed into it, so names must not collide and each resource has exactly one owner. The `default` CLI profile is never used; Person 1 uses profile `intentlock` (IAM user `aparpia`).
| Owner | Stack | Resources |
|---|---|---|
| Person 1 | `intentlock-control-plane-dev` | DynamoDB `intentlock-control-plane`; Lambda `intentlock-authorizer`; log group `/aws/lambda/intentlock-authorizer`; IAM role + 2 managed policies (generated names). All tagged `Project=IntentLock`, `Owner=person1-control-plane`. |
| Person 2 (proposed) | `intentlock-execution-plane-dev` | S3 artifact/evidence bucket `intentlock-artifacts-247495661854-us-east-1` (account+region suffix keeps the global name unique); promoter/sandbox resources prefixed `intentlock-exec-`; tags `Owner=person2-execution-plane`. |
| Shared (once) | n/a | Lambda-zip bucket for the control plane `intentlock-code-247495661854-us-east-1` (Person 1 creates it only after approval); ONE AWS Budget `intentlock-hackathon` (owner/Kavin creates it). |
Rules: never create unprefixed resources; never edit the other person's stack; check names with `verify_aws_readonly.sh` first (it checks the Person 1 names); the Lambda concurrency quota and every cost are shared across both stacks (re-check the quota in THIS account once the profile works).

### Human review authentication

Set `INTENTLOCK_REVIEWER_TOKENS` to a JSON object mapping reviewer names to
random, private tokens. Approval, rejection and promotion requests must send
`X-IntentLock-Reviewer-Token` for the reviewer named in the body. Missing or wrong
tokens return 401; an unset mapping fails closed. Enter the token into the Impact
Review screen at review time. Do not store it in a `VITE_*` variable, commit it,
or expose these endpoints over plaintext HTTP outside localhost. The existing
`INTENTLOCK_REVIEWERS` list can further restrict which authenticated reviewers
may act. A stopped run blocks approval and promotion; an already started promoter
must separately verify current run state before applying a patch.

## Verified 2026-11-07 with profile `intentlock` (read-only) — IAM permissions are MISSING
- **Identity verified:** account `247495661854` (matches the shared account), IAM user `aparpia`, region `us-east-1`.
- **Service access: none.** `verify_aws_readonly.sh` (13 of 15 checks FAIL, both identity checks PASS) got `AccessDenied(Exception)` for every call: DynamoDB `ListTables`/`DescribeLimits`, Lambda `GetAccountSettings`/`ListFunctions`, S3 `ListAllMyBuckets`, CloudFormation `ListStacks`/`ValidateTemplate`, Logs `DescribeLogGroups`, CloudWatch `ListMetrics`, Bedrock `ListFoundationModels`/`ListInferenceProfiles`, Budgets, and `iam:SimulatePrincipalPolicy`. So: whether `intentlock-control-plane-dev` or the planned names already exist, which Bedrock models are available, and the Lambda quota are all UNKNOWN. The user currently appears to have no attached policy granting these actions.
- **Fix (needs the account owner, Kavin; Person 1 does not and will not change IAM):** review and attach, to user `aparpia`, the two least-privilege policies in `iam/`. IMPORTANT: create BOTH as **customer-managed policies** (IAM → Policies → Create policy → JSON), then attach them to the user. Do NOT paste them as inline policies: inline policies for one user share a 2,048-character total, and file 02 (about 3,150 characters) will not fit (managed limit is 6,144):
  - `iam/01-readonly-verification.json` — list/describe reads for the checks above plus self-simulation; no write actions, no model invocation.
  - `iam/02-deploy-control-plane.json` — create/update/delete only for stack `intentlock-control-plane-dev`, table `intentlock-control-plane`, function `intentlock-authorizer`, its log group, IAM roles/policies prefixed `intentlock-control-plane-dev-` (with `iam:PassRole` limited to Lambda), the Lambda-zip bucket `intentlock-code-247495661854-us-east-1`, and an optional `intentlock-*` budget. All scoped to account `247495661854` / `us-east-1` where the service is regional.
  - Bedrock `InvokeModel` is deliberately NOT included (billable); add it only after a model is chosen and approved. Alternatively Kavin can deploy the stack himself with his own credentials.
  - `DeleteTable` is included so a failed first deploy can roll back cleanly; remove it if you prefer tables never be deleted by this user.
After attaching, re-run: `LIST_MODELS=1 CODE_BUCKET=<name> PROFILE=intentlock EXPECTED_USER=aparpia EXPECTED_ACCOUNT=247495661854 infrastructure/control-plane/verify_aws_readonly.sh`.

### Update after policy 01 was attached (2026-11-07)
- **`01-readonly-verification.json` is in effect: `verify_aws_readonly.sh` now passes 15 of 15.** Verified in the shared account, `us-east-1`: stack `intentlock-control-plane-dev` does not exist; table `intentlock-control-plane`, function `intentlock-authorizer` and log group `/aws/lambda/intentlock-authorizer` are free; the account has no budgets yet.
- **`02-deploy-control-plane.json` is NOT attached.** It failed as an inline policy (size limit above). The IAM simulation shows `implicitDeny` for every deploy action until it is created as a managed policy and attached.
- **Bedrock (metadata only, nothing invoked):** Anthropic models are listed; `get-foundation-model-availability` reports `authorizationStatus=AUTHORIZED`, `entitlementAvailability=AVAILABLE`, `regionAvailability=AVAILABLE` for `anthropic.claude-haiku-5-5` and `anthropic.claude-sonnet-5-5` (`agreementAvailability=NOT_AVAILABLE`, meaning no separate agreement is offered/required per the API; not proof of invocation). `anthropic.claude-sonnet-5-5` supports only `INFERENCE_PROFILE` invocation, so the id to configure is the profile id (for example `us.anthropic.claude-sonnet-5-5`), not the bare model id. Cross-region profiles (`us.`/`global.`) may serve requests from other US regions; for strict single-region use verify an in-region option first. One `bedrock:GetFoundationModel` call was denied for the haiku id while the same call succeeded for sonnet (possibly IAM propagation); the availability API worked for both. A model id is still an owner decision; for cheap contract drafting `us.anthropic.claude-haiku-5-5` is the likely choice. Not yet confirmed by an actual (billable) invocation.

### If the IAM console says "policyDocument ... must contain only printable ASCII characters"
The `iam/*.json` files are pure ASCII (checked); the error means the text picked up an invisible or smart character while being copied from a rendered page or chat. Use `iam/02-deploy-control-plane.min.json` (single line, no indentation, 3,156 characters, printable ASCII only) and copy it straight from the file or with `pbcopy < infrastructure/control-plane/iam/02-deploy-control-plane.min.json` instead of from a screen. In the console paste into **Policies > Create policy > JSON** (replace everything in the editor) and create it as a customer-managed policy.

### Update: full access in place (2026-11-07) — deployment READY pending approvals
- `verify_aws_readonly.sh`: **15 of 15 pass**; the IAM simulation now shows `allowed` for CloudFormation Create/Update, IAM CreateRole/PassRole/PutRolePolicy/AttachRolePolicy/CreatePolicy, DynamoDB CreateTable, Lambda CreateFunction/UpdateFunctionCode, Logs CreateLogGroup, S3 CreateBucket/PutObject (simulation is evidence only; the deployment is the real proof).
- **How:** a read-only `iam list-attached-user-policies` shows `aparpia` has the **AdministratorAccess** managed policy (no inline policies, no groups) — NOT the scoped `iam/` policies. That is far broader than needed: with it, a mistake affects Kavin's whole account. Recommendation: after the first successful deploy, replace AdministratorAccess with the two scoped policies (`iam/01`, `iam/02`, both as managed policies). The template itself only creates the resources listed above and tags them.
- **Still unverified:** the real deployment, Lambda behaviour on AWS, real-table store tests, real DynamoDB latency, Bedrock invocation, and that the IAM policies in `template.yaml` are sufficient at runtime (moto does not enforce IAM).
- **Proposed deployment sequence (NOT executed; needs explicit approval):** (1) create private bucket `intentlock-code-247495661854-us-east-1` in us-east-1 with public access blocked; (2) `build_lambda.sh`, upload the zip; (3) `aws cloudformation deploy --profile intentlock --region us-east-1 --stack-name intentlock-control-plane-dev --capabilities CAPABILITY_IAM ...`; (4) opt-in real-table store tests (`INTENTLOCK_AWS_TEST_TABLE`) and one Lambda invoke with a synthetic request; (5) optionally a $25 budget `intentlock-hackathon`. Estimated cost of all of this: cents (see Deployment readiness).

## DEPLOYED 2026-11-07 (owner-approved) — control plane in the shared account
Account `247495661854`, region `us-east-1`, profile `intentlock` (IAM user `aparpia`, AdministratorAccess), code deployed from git `3105520`.
| Resource | Identifier | Verified |
|---|---|---|
| S3 bucket (Lambda artifact) | `intentlock-code-247495661854-us-east-1` (not in the stack; created by hand) | all four public-access blocks on, SSE-S3 (AES256), TLS-only bucket policy, `PolicyStatus.IsPublic=false`, tags Project/Owner; no versioning |
| Lambda zip | `s3://intentlock-code-247495661854-us-east-1/intentlock-authorizer.zip` (2,790,965 bytes, SSE AES256, sha256 `3831dad5a53b82b9cad9c8890324c16de92b80d9bf0e8cf564dcde49d18c5dbf`) | contains only `intentlock_api/{__init__,store,dynamo_store,service,lambda_handler}.py` + `intentlock_policy` + pydantic; no app/Bedrock/FastAPI/launcher/impact/promoter code |
| CloudFormation stack | `intentlock-control-plane-dev` (`arn:aws:cloudformation:us-east-1:247495661854:stack/intentlock-control-plane-dev/0059c420-dafd-11f1-9d5c-0afff8ef71a5`), `CREATE_COMPLETE` | created through a reviewed change set containing exactly 6 `Add` resources |
| DynamoDB table | `intentlock-control-plane` (`arn:aws:dynamodb:us-east-1:247495661854:table/intentlock-control-plane`) | ACTIVE, PAY_PER_REQUEST, keys pk/sk, SSE enabled, PITR enabled, tags |
| Lambda function | `intentlock-authorizer` (`arn:aws:lambda:us-east-1:247495661854:function:intentlock-authorizer`) | python3.12, x86_64, 256 MB, 10 s, handler `intentlock_api.lambda_handler.handler`, env `INTENTLOCK_STORE=dynamodb`, `INTENTLOCK_TABLE=intentlock-control-plane`; no Function URL, no resource policy |
| Log group | `/aws/lambda/intentlock-authorizer` | 30-day retention; log stream written by the function |
| IAM role | `intentlock-control-plane-dev-AuthorizerRole-qgkrDMazxU83` | trust `lambda.amazonaws.com`; permissions exactly: table `GetItem/PutItem/UpdateItem/Query` (managed policy `…-TableAccessPolicy-75VDV3Aia9fp`) and `logs:CreateLogStream/PutLogEvents` on its log group; no Bedrock/S3/other |
| IAM managed policy | `arn:aws:iam::247495661854:policy/intentlock-control-plane-dev-InvokePolicy-GKIiNRNCjoTK` | invoke this one function; attach to a principal that should call it |
Not created: Function URL, budget, any Person 2 resource. Not changed: IAM permissions of any user.

### REAL AWS verification results (these are NOT mocks)
- **Real DynamoDB tests** (`INTENTLOCK_AWS_TEST_TABLE=intentlock-control-plane AWS_PROFILE=intentlock pytest apps/api/tests -k dynamodb-real`): **87 passed in one clean run** (store contract 15, API 25, impact/review 30, launcher 17), including the concurrency tests (racing run transitions = one winner; exact tool-call reservation; gap-free event sequences; 12 concurrent authorizations vs a limit of 5). A FIRST run had 64 pass / 23 fail: all 23 in `test_impact_api.py`, caused by tests reusing the fixed id `impact_01`, which collides on a persistent table (moto starts empty each test). Fixed by giving every test unique impact ids; not a store defect. Test data was written with unique ids into the real table; the table was empty before (consistent scan = 0) and was emptied afterwards (1,252 items, all matching test-key patterns, deleted; 0 left).
- **Real Lambda synthetic authorization** (deployed function, via `lambda:Invoke`): allow in-contract read; `.env` → `PROTECTED_PATH` with `stop_run=true` and the run committed as `blocked` in DynamoDB; replay → `RUN_NOT_ACTIVE`; events gap-free and all `decided`; stored events contain no file contents/secrets; CloudWatch audit lines present (INFO enabled), carrying decision metadata only.
- **Fail-closed on the real Lambda:** malformed event, non-object event, unknown run, wrong contract version, unknown tool, unapproved test id, path traversal, path outside allowed paths — all denied with the expected reason codes and no function error. **NOT tested on real AWS:** behaviour when DynamoDB itself is unavailable or the function is misconfigured (covered only by mocked tests; the code path denies).
- **Measured latency (from this laptop to `us-east-1`, N=30 sequential, warm):** Lambda invoke client wall time p50 237 ms / p95 242 ms / max 243 ms; Lambda function duration (includes all DynamoDB calls) p50 146 ms / p95 154 ms / max 155 ms (billed ~144 ms avg); policy-engine decision time alone 0.2 ms; **API-on-laptop path** (`service.authorize_request` laptop→DynamoDB, what Person 2's MCP client would hit if the API runs locally with `INTENTLOCK_STORE=dynamodb`) p50 649 ms / p95 870 ms / max 1,078 ms. **Cold start:** first invocation 5.0 s client wall (3.83 s function duration + 0.57 s init).
- Default (mocked) suite after changes: 361 passed, 7 skipped.

### Findings and risks from the deployment
1. **Cold start ~5 s and 256 MB:** if anything calls the Lambda with a short timeout the first call will fail closed (deny). Raising memory (e.g. 512–1024 MB) would cut it; needs a stack update (not done; account Lambda concurrency quota is small, so provisioned concurrency is not an option to assume).
2. **Person 2 cannot reach the deployed Lambda:** it has no Function URL and Person 2's client speaks HTTP with a shared token, not AWS-signed requests or `lambda:Invoke`. Their MCP server must keep targeting the HTTP API (which can run with `INTENTLOCK_STORE=dynamodb` against this table). Their 2 s client timeout vs API-on-laptop max 1.08 s leaves little margin; run the API in-region or keep the memory store for live demos if latency matters.
3. **AdministratorAccess** is still attached to `aparpia` (broader than needed); swap for the scoped policies in `infrastructure/control-plane/iam/` when convenient.
4. The role's log-stream ARN is `…:log-group:/aws/lambda/intentlock-authorizer:*:*` (the template appends `:*` to an ARN that already ends in `:*`); it works (logs were written) but should be tidied in a future stack update.
5. **Budget NOT created:** requires Kavin's agreement on the alert recipient; none exists yet (account has 0 budgets). A budget is an alert, not a spending cap. Estimated spend for everything above: well under $1 (about 1,300 table items written/deleted, ~100 Lambda invocations, small logs); not verified against the bill.
6. Bedrock: see below; nothing invoked.

### Bedrock inspection (read-only; nothing invoked, no cost)
All 13 ACTIVE Anthropic models in `us-east-1` are invocable ONLY through an inference profile (`inferenceTypesSupported=INFERENCE_PROFILE`), so the configured id must be a profile id. `us.anthropic.claude-haiku-5-5` is a `SYSTEM_DEFINED`, `ACTIVE` profile that routes across `us-east-1`, `us-east-2` and `us-west-2` (cross-region: data may be processed in those regions). For `anthropic.claude-haiku-5-5`, `anthropic.claude-haiku-4-5-20251001-v1:0` and `anthropic.claude-sonnet-5-5` the availability API reports `authorizationStatus=AUTHORIZED`, `entitlementAvailability=AVAILABLE`, `regionAvailability=AVAILABLE` (`agreementAvailability=NOT_AVAILABLE`, i.e. no agreement offered/required per the API). All stream and output TEXT. **Recommendation (owner decides): `us.anthropic.claude-haiku-5-5`** as the cheapest/fastest tier by Anthropic's usual tiering, set as `INTENTLOCK_BEDROCK_MODEL_ID`. **Not verified:** actual price (the Price List API has no Bedrock entry; check the Bedrock pricing page), latency, JSON-output quality, that `temperature=0` is accepted, or that an invocation succeeds (that is billable and needs approval).

## Integration path, latency fix and secure demo run (2026-11-07)
**How Person 2's MCP server reaches the deployed table:** `MCP server --HTTP + X-IntentLock-Internal-Token--> FastAPI API (localhost) --boto3--> DynamoDB table intentlock-control-plane`. The deployed Lambda is NOT in this path (it has no URL; their client speaks HTTP+token only). Person 2 sets `INTENTLOCK_API_URL=http://127.0.0.1:8787` (their client only allows localhost http or https) and the same `INTENTLOCK_INTERNAL_TOKEN`; no change to their code.

**Latency investigation (real AWS, laptop -> `us-east-1`, same tool before and after: `measure_authorize_latency.py`):**
| | before | after |
|---|---|---|
| DynamoDB calls per authorization | 9 | 6 |
| first authorization (session/TLS setup) | 1,509 ms | 514 ms (443 ms over HTTP with the startup warm-up) |
| steady p50 / p95 / max | 759 / 1,061 / 1,134 ms | 436 / 529 / 662 ms |
| headroom vs Person 2's 2,000 ms timeout (at max) | 1.8x | 3.0x |
Measured over the real HTTP path through the demo launcher (fresh connection per request, 2 s timeout, N=40): first 442 ms, steady p50 438 / p95 487 / max 495 ms, 0 timeouts. Raw DynamoDB GetItem round trip from this laptop is ~69-73 ms, so the floor with 6 sequential calls is ~420 ms; the remaining cost is network distance, not CPU (the policy engine itself takes 0.2 ms).
Fixes (my code only, nothing of Person 2's touched): load the run and contract once (expiry reuses them); skip the read before each event update (`update_event(base=...)`); remember the last event sequence per run (`event_seq` condition still guarantees exactness; a stale hint just costs one extra retry); one shared thread-safe boto3 client instead of per-thread sessions (each new worker thread paid setup cost); startup warm-up; bounded connect/read timeouts; explicit bounded retries for `TransactionConflictException` (a plain write to the run item during an in-flight event transaction; surfaced as a real failure in the first real-table run after the change and fixed). Remaining options (NOT done): run the API in-region (EC2/ECS) ~5-10 ms per call, authorization ~60 ms (new AWS resources, needs approval); redeploy the Lambda code (code-only update) to pick up the same optimizations (the deployed Lambda still runs the earlier code); cache the immutable contract. Person 2's supervisor polls `GET /api/runs/{id}` every ~200 ms and each poll costs 2 DynamoDB calls (~140 ms), which is fine for one demo run.
**Rule of thumb for the demo:** run `AWS_PROFILE=intentlock .venv/bin/python infrastructure/control-plane/measure_authorize_latency.py` on the demo machine and network first. If steady p95 stays below ~1,000 ms use `INTENTLOCK_STORE=dynamodb`; if the venue network is worse, fall back to `INTENTLOCK_STORE=memory` (clearly labelled "NOT DynamoDB") and show the deployed stack separately.

**Run the API securely for the demo — `apps/api/run_demo_api.sh`** (validates config, refuses unsafe settings, `--check` validates without starting):
```bash
export INTENTLOCK_INTERNAL_TOKEN=$(openssl rand -hex 24)                      # give the same value to Person 2
export INTENTLOCK_REVIEWER_TOKENS="{\"demo-user\":\"$(openssl rand -hex 24)\"}"  # one strong, unique token per reviewer
INTENTLOCK_STORE=dynamodb AWS_PROFILE=intentlock apps/api/run_demo_api.sh
```
It refuses to start when: the internal token is missing/short/placeholder; reviewer tokens are missing/weak/shared; the bind host is not loopback (the public API is unauthenticated); in DynamoDB mode the profile is unset or `default`, the region is not `us-east-1`, or the profile is not account `247495661854`. Minimum demo checklist: localhost only; strong tokens; `intentlock` profile; CORS limited to the dashboard origin (default `http://localhost:5173`); do not expose the API through a tunnel or public IP; reviewers enter their token in the dashboard only.
**Credentials for the API process (decision needed; no IAM changed):** (A) today: profile `intentlock` (AdministratorAccess) on a trusted laptop, localhost only. Works, but the API then holds admin credentials. (B) recommended, needs the account owner's approval: a dedicated IAM user whose only permission is the stack's existing managed policy `intentlock-control-plane-dev-TableAccessPolicy-75VDV3Aia9fp` (GetItem/PutItem/UpdateItem/Query on the one table; this is exactly what the deployed Lambda role runs on and passed the real Lambda tests), with its key entered privately via `aws configure --profile intentlock-api`. Owner/Kavin runs, then deletes the user after the demo: `aws iam create-user --user-name intentlock-api-demo`, `aws iam attach-user-policy --user-name intentlock-api-demo --policy-arn arn:aws:iam::247495661854:policy/intentlock-control-plane-dev-TableAccessPolicy-75VDV3Aia9fp`, `aws iam create-access-key --user-name intentlock-api-demo` (the key is shown once; enter it only in the terminal). (C) run the API inside AWS with an instance role (also fixes latency); new resources, needs approval.
**Budget (NOT created):** one email address was given in chat and is presumed to be Kavin's, but it was not confirmed as the agreed recipient and the second recipient (Ali) was not given. Email addresses are intentionally not stored in the repo. Needs: confirmation that Kavin agreed, both addresses. Reminder: a budget alerts, it does not cap spending.
