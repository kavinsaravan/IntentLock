# IntentLock demo runbook

This is a **localhost** demo for security, platform, and DevOps teams that use coding agents. The dashboard, Kiro, Docker, API, and promoter run on one Mac. The demo uses a temporary copy of `demo/repository`, in-memory control-plane state, and local artifacts. Never describe it as a deployed AgentCore or production service.

## Before judges arrive

1. Start Docker Desktop. Check that `kiro-cli whoami` succeeds. Have Python 3.12+ and Node/npm installed.
2. From the repository root, install dependencies if needed:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r apps/api/requirements-dev.txt
   cd apps/web && npm ci && cd ../..
   ```

3. In a private terminal, create fresh local secrets. Do not put them in a committed file or show this terminal during the presentation:

   ```bash
   export INTENTLOCK_INTERNAL_TOKEN="$(openssl rand -hex 24)"
   export INTENTLOCK_PROMOTER_TOKEN="$(openssl rand -hex 24)"
   export INTENTLOCK_REVIEWER_TOKEN="$(openssl rand -hex 24)"
   export INTENTLOCK_REVIEWER_TOKENS="{\"demo-user\":\"$INTENTLOCK_REVIEWER_TOKEN\"}"
   ```

4. For the live Bedrock proposal, set an AWS profile with Bedrock access and the verified model ID:

   ```bash
   export AWS_PROFILE=hackathon
   export AWS_REGION=us-east-1
   export INTENTLOCK_BEDROCK_MODEL_ID=us.anthropic.claude-haiku-5-5
   ```

   Bedrock is optional to starting the stack. If it is unavailable, use the dashboard's clearly labelled fixture draft. The fixture still requires approval and runs through the same deterministic policy and Impact Preview checks.

5. Run the preflight and start the stack in that same terminal:

   ```bash
   bash demo/start_local_demo.sh --check
   bash demo/start_local_demo.sh
   ```

   Keep the terminal open. The script builds `intentlock-impact-preview:local`, creates a **fresh** temporary Git repository, starts the promoter on `127.0.0.1:8790`, the API on `127.0.0.1:8787`, and the dashboard on `http://localhost:5173/`. It prints the fresh base commit and private log directory. It does not print tokens. If using a screen recorder, show only the browser after startup.

   The script refuses to start (exit code 2, including with `--check`) if any of ports 8787, 8790, or 5173 is already in use, and names the process holding it. Stop the earlier stack first. To run a second stack beside it, set `INTENTLOCK_DEMO_API_PORT`, `INTENTLOCK_DEMO_PROMOTER_PORT`, and `INTENTLOCK_DEMO_WEB_PORT` to free ports.

## Five-minute judge walkthrough

**Prompt 2 proof:** Judges should see all four required actions: launch Kiro, watch live tool traffic, deny a disallowed request, and press **Stop run**. The recorded unsafe-then-safe patch flow below demonstrates Impact Preview's additional value, but does not itself show the protected-path denial or a manual Stop. Rehearse a short separate run that requests `.env` and show `PROTECTED_PATH` in Agent activity; that denial automatically blocks its run. Start another run and press **Stop run** while it is active, then show the stopped state. Automated tests verify that later controlled requests receive `RUN_NOT_ACTIVE`; do not claim a later live request occurred unless it appears in the current run's audit. Keep the [previously verified patch screenshots](demo-evidence/README.md) available if Kiro takes a different path live. State clearly when evidence comes from a different run.

| Time | Show | Say |
| --- | --- | --- |
| 0:00–0:35 | Create mission | “Coding agents help us ship, but security teams need proof of what they were allowed to do and what a patch would break.” |
| 0:35–1:20 | Generate contract, then review | “Bedrock drafts the mission. A human approves it; Bedrock does not make the access decision.” Point to allowed paths, protected paths, fixed `npm run test:auth`, and disabled network. |
| 1:20–2:15 | Approve and launch | “Real Kiro gets only IntentLock MCP tools. The policy checks each call and records the result.” Show the live run and an allowed read. |
| 2:15–3:15 | Unsafe patch report | “Impact Preview ran the proposed edit in Docker. The fixed auth test failed, so approval is disabled and the real repository is unchanged.” |
| 3:15–4:20 | Corrected patch and human review | Select the passing attempt. Point to the diff and SHA-256; enter the private reviewer token, click **Approve exact patch**, then **Request promotion**. |
| 4:20–5:00 | Promotion and audit | Show `PROMOTED` and the final audit. “The promoter rechecked the run, base commit, review, and exact patch bytes before applying it.” |

Use the task field to describe the timeout bug. For the full unsafe-then-safe live sequence, edit the contract’s **Mission summary** before approving it to instruct Kiro to read `src/auth/session.ts`, submit an auth-bypass proposal first, then submit a separate patch changing `>= timeoutSeconds;` to `>= timeoutSeconds * 1000;` without editing tests. Impact Preview must block the first proposal. Kiro is nondeterministic: rehearse this sequence and keep the verified [unsafe](demo-evidence/intentlock-full-unsafe.png) and [promoted](demo-evidence/intentlock-full-promoted.png) screenshots available if the live agent takes a different route. Do not claim a screenshot came from the live attempt if it did not.

The dashboard defaults to reviewer `demo-user`. Use the `INTENTLOCK_REVIEWER_TOKEN` generated in your private terminal for the masked reviewer-token field. The fresh base commit is prefilled by the startup script. On the impact page, select the **earlier** attempt to show the blocked patch and the **latest** attempt for the corrected patch. If the current run has one attempt, use the recorded evidence for the other case.

## Recording checklist

- Record one continuous browser sequence from mission creation through promotion. Keep terminal windows, tokens, AWS credentials, and agent logs out of the recording.
- Show the unsafe report’s failing fixed test and disabled approval, then the corrected report’s passing test, reviewed diff, and promoted state.
- Capture the final audit after promotion. The previous verified browser run and Bedrock screenshot are documented in [demo evidence](demo-evidence/README.md).
- Stop an active run in the dashboard before closing the stack. Press Ctrl-C in the startup terminal afterward. The private temporary repository, artifacts, and logs remain under the printed temporary path for review.

## If something fails

| Symptom | Check | Fallback |
| --- | --- | --- |
| Bedrock proposal fails | AWS profile, region, model access | Use **fixture draft** and label it as such. |
| Kiro does not launch | `kiro-cli whoami`; dashboard launcher state; private agent log | Show the recorded real-Kiro evidence and explain the live setup issue. |
| Docker preview fails | Docker Desktop and `docker info`; image build | Do not approve or promote. Show the recorded blocked/passing impact evidence. |
| Reviewer action fails | Reviewer name `demo-user` and the private token | Retry with the same token; never bypass the review endpoint. |
| Startup says a port is already in use | The process named in the message; `lsof -nP -iTCP:8787 -sTCP:LISTEN` | Stop the earlier demo stack and retry, or set the `INTENTLOCK_DEMO_*_PORT` variables. Do not use a stack you did not just start. |
| API or UI unavailable | Printed private API/web logs; `http://127.0.0.1:8787/health` | Use the screenshots and architecture explanation. |

The previous verified [browser flow](demo-evidence/README.md), [Person 2 handoff](handoffs/person2-execution-plane.md), and [sandbox README](../packages/sandbox-provider/README.md) contain the evidence and deeper implementation details. S3, DynamoDB, Lambda, and CloudWatch were checked separately; the local browser recording does not combine those AWS services in one deployed path.
