# Verified local demo evidence — 2026-11-07

These screenshots are from actual headless Chrome interactions with the local dashboard and API. The Bedrock proposal used the `hackathon` profile in `us-east-1` and the active `us.anthropic.claude-haiku-5-5` inference profile. The screenshot shows a live draft with the executable `auth-tests` catalog (`npm run test:auth`).

The unsafe and promoted screenshots come from one browser-driven run, `run_f7211e0c76a8`: the dashboard launched real authenticated Kiro; Kiro made an authorized read and proposed two separate patches; Docker TwinGuard blocked the auth-bypass patch on the fixed auth test; the reviewer selected that earlier attempt and saw approval disabled; then selected the corrected timeout patch, approved its exact hash, and requested promotion. The local promoter applied the corrected patch, and the immutable final audit included the promotion event. The test script also checked the Git repository contained the corrected line.

- `intentlock-bedrock-draft.png`: real Bedrock proposal with the fixed Node auth-test catalog displayed in the dashboard.
- `intentlock-full-unsafe.png`: earlier unsafe attempt blocked by TwinGuard, approval disabled.
- `intentlock-full-promoted.png`: corrected attempt passed, reviewed, and promoted in the same run.

This run used the local in-memory control-plane store and local artifact directory. It is evidence for the local product flow, not for deployed DynamoDB/Lambda, AgentCore Gateway, Code Interpreter, or S3 in that same run. Earlier S3 tests are recorded in the Person 2 handoff.

After this browser run, the existing AWS authorizer Lambda was updated with CloudWatch embedded metrics and verified separately with a synthetic denied invocation. These screenshots do not depict that AWS invocation.
