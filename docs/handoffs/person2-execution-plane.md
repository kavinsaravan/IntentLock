# Handoff — Person 2 execution plane

Updated: 2026-11-07. Branch: `codex/person2-execution-plane`.
Commit: use `git log -1 -- docs/handoffs/person2-execution-plane.md` after this milestone commit.
Scope: Kiro CLI coding agents for security/platform/DevOps teams; broader agents deferred.

## Completed

- Read-only stdio MCP adapter (`apps/mcp-server/server.py`), bounded UTF-8 reads,
  path traversal/symlink/protected-path defenses, authorization before filesystem access.
- HTTP integration with Person 1: `INTENTLOCK_API_URL`, `INTENTLOCK_INTERNAL_TOKEN`,
  `X-IntentLock-Internal-Token` header. Reject redirects, non-local plaintext HTTP,
  malformed schemas, inconsistent allows and mismatched request IDs.
- Kiro 3.1 profile/launcher: only IntentLock read tool, clean temporary working dir.
- Process supervisor registers PID at `/internal/runs/{id}/agent`, polls `/api/runs/{id}`,
  terminates process group on terminal/unavailable state, emits sanitized lifecycle event.
- SandboxProvider interface and TypeScript demo bug fixture (not sandbox implementation).

## Schema and ownership decisions

Accept all six schema 0.1.0 deviations listed in Person 1 handoff §8 for this prototype.
No shared schemas or Person 1 source files were changed. Owned schema snapshots mirror
his canonical request/decision/run files for independent branch operation. Refresh in
coordination; canonical authority remains `packages/contracts` after integration.

Use authenticated HTTP, not direct Lambda invocation. Person 2 will emit impact and
promotion events after implementing them; Person 1 owns authorization/run events.
`pass` never authorizes automatic promotion. Permission-request and finish tools are
not exposed yet. AgentCore interceptor mapping is deferred until Gateway integration.

## Verified

15 tests passed, including 2 real localhost HTTP cross-workstream tests and 12
parameterized subtests. Integration used an isolated archive of `origin/Ali-Person-1`:
MCP subprocess → actual API → ALLOW, PROTECTED_PATH, RUN_NOT_ACTIVE; checked events
contain decisions without file contents. Stop test uses a real sleeping subprocess
and verifies it is gone after API stop (not a live Kiro model session).

Actual Kiro smoke attempted: binary launches but reports **Not logged in**. No live
model-generated tool call was observed. Run `kiro-cli login`, then the documented
`tests/sandbox/kiro_smoke.py` opt-in test. No account credentials were changed.

## Known limits / next steps

- Live Kiro authentication required; actual tool exposure must be inspected in session.
- Docker absent; no sandbox, patch inspection or promotion implemented yet.
- No AgentCore/S3 or AWS deployment yet.
- Read is the only exposed tool. List/search need per-result policy filtering.
- Polling has 200 ms spacing and 2-second HTTP timeout, so no sub-second SLA claimed.
- Local process timeout kills the agent and emits an event; API has no execution-plane
  terminal-state transition endpoint. Agree on a future completed/failed API rather
  than treating an ingestion event as an authoritative state transition.
- Trusted regression tests/test command configuration must be outside writable scope.
- Kiro configuration is not OS containment; review global auto-loaded context before
  claiming all repository access is mediated.

See `apps/mcp-server/README.md` for launch and test commands. Person 1 can merge/cherry-pick
this branch after review; no control-plane files are included in this commit.
