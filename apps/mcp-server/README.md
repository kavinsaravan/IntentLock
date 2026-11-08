# IntentLock Kiro MCP execution plane

The launcher gives Kiro an IntentLock-only MCP profile for the seven frozen PRD tools (list, read, literal search, patch proposal, approved test, permission request, and task summary) plus `run_approved_command`. Each tool call is authorized by the control plane. Listings and search results are filtered against the active contract's allowed and protected paths. `@intentlock/propose_patch` previews a patch in Docker without modifying the protected repository. `run_approved_test(test_id)` and `run_approved_command(command_id)` execute only commands from the administrator catalogue (`packages/impact-analyzer/catalogue.json`) that the active contract approves, in a networkless Docker snapshot of the clean base commit. The agent supplies an ID only, never argv or a shell string; unknown IDs and extra keys are denied before anything runs. `run_approved_test` is limited to `test`-kind checks; `run_approved_command` also runs `static` checks such as `typecheck`. Permission requests and summaries become immutable evidence; a permission request does not grant access. See [the sandbox setup](../../packages/sandbox-provider/README.md).

For each active contract, MCP `tools/list` advertises only its allowed tools plus the always-available permission request and task summary tools. The approved check IDs are included in the relevant descriptions. This helps Kiro choose valid tools; the control plane still authorizes every call. If contract lookup fails, the adapter falls back to plain descriptions and calls remain subject to fail-closed authorization. The launcher tells Kiro to stop making tool calls after `TOOL_CALL_LIMIT_EXCEEDED`; the contract's hard limit is still enforced by the policy engine.

Standalone `run_approved_test` and `run_approved_command` execute on the original clean base commit. Their results do not measure a proposed patch. Impact Preview automatically runs the selected approved checks on a proposed patch and returns their results in its impact report; after a passing report the agent should finish so a human can review and promote the exact patch.

Set `INTENTLOCK_API_URL` to the localhost control plane, `INTENTLOCK_INTERNAL_TOKEN` to its internal token, and either `INTENTLOCK_ARTIFACT_DIR` or `INTENTLOCK_ARTIFACT_BUCKET`. For S3, also set `INTENTLOCK_AWS_PROFILE` if needed. The launcher passes only configured artifact settings to the MCP server; unset values are never forwarded as literal placeholders. Build the sandbox image and configure the promoter before proposing patches.

After creating an approved contract and active run, launch:

```sh
.venv/bin/python -B apps/mcp-server/launch_kiro.py --repository /absolute/path/to/demo-repo --run-id RUN_ID --contract-id CONTRACT_ID --contract-version 1
```

Authenticate Kiro with `kiro-cli login` first. `KIRO_CLI_BINARY` can select an absolute binary path. `--print-profile` shows the generated profile without launching Kiro. The launcher selects Kiro V3 and starts the agent in a new process group. Its supervisor registers the PID, polls run state, handles SIGTERM, cleans up the process group, and reports `completed` or `failed` through the control plane's terminal-state endpoint. A stopped or blocked run stays terminal. If group signaling is denied, it falls back to signaling the direct process and reports failure; descendant cleanup then cannot be guaranteed. With artifact storage configured, the supervisor exports the immutable final audit after its terminal event.

The control-plane HTTP timeout is two seconds, so polling is not a guaranteed sub-second stop mechanism. Tool authorization is checked separately on every call. Kiro itself runs outside Docker; Docker isolates the patch preview, not the Kiro process.

## Verification

```sh
INTENTLOCK_PERSON1_CHECKOUT=/absolute/path/to/isolated/Ali-checkout .venv/bin/python -B -m unittest discover -s tests/sandbox -v
```

The suite exercises the real local control-plane HTTP API, SIGTERM, stopped-run precedence, Docker isolation and patch promotion. The full authenticated Kiro workflow is opt in:

```sh
INTENTLOCK_PERSON1_CHECKOUT=/absolute/path/to/isolated/Ali-checkout INTENTLOCK_REAL_KIRO=1 INTENTLOCK_KIRO_GENERATE_PATCH=1 .venv/bin/python -B -m unittest discover -s tests/sandbox -p test_impact_preview_api.py -k real_kiro -v
```

Set `INTENTLOCK_ARTIFACT_BUCKET` and `INTENTLOCK_AWS_PROFILE` to exercise live S3 evidence. The opt-in test uses real Kiro and MCP, Docker preview, human-review API calls and exact-hash promotion against a temporary Git repository. It does not automate browser clicks in the dashboard.
