# AGENTS.md — IntentLock operating guide for AI coding agents and humans

Tool-agnostic (Claude Code, Codex, Cursor, humans). Do not rely on chat history; everything needed is in the repo.

## Project
IntentLock secures autonomous coding agents on AWS with two guarantees:
1. **IntentLock** — Bedrock *proposes* a mission contract, a human approves it, and a deterministic engine authorizes every controlled tool request (unauthorized calls are rejected before the tool runs).
2. **Impact Preview** — authorized mutations run in an isolated sandbox first; impact is analyzed, tests run, a SHA-256-addressed patch is produced, and only the exact human-approved patch is promoted after base-commit + hash verification.

## Source of truth
Read `intentlock-prd.md` (product scope, security model, schemas, acceptance criteria). Do not expand scope beyond it (no new languages, tools, network, sandbox capabilities).

## Workstreams and branches
| Workstream | Owner | Branch | Owns |
|---|---|---|---|
| A — control plane | Person 1 (Ali) | `Ali-Person-1` | `apps/web`, `apps/api`, `packages/policy-engine`, `infrastructure/control-plane`, `tests/contract`, `tests/policy`, `docs/architecture.md`, `docs/handoffs/person1-control-plane.md` |
| B — execution plane | Person 2 | `person2-execution-plane` (confirm actual name) | `apps/mcp-server`, `packages/sandbox-provider`, `packages/impact-analyzer`, `packages/patch-promoter`, `infrastructure/execution-plane`, `demo/repository`, `tests/sandbox`, `docs/threat-model.md`, `docs/handoffs/person2-execution-plane.md` |

`main` is the stable integration branch. Never commit to `main` or to the other person's branch. Operate only inside your workstream's paths.

## Handoff protocol
Before starting, read whichever exist: `docs/handoffs/person1-control-plane.md`, `docs/handoffs/person2-execution-plane.md`. Each agent edits only its own workstream's handoff. Before ending a session, update it: status, verification results, blockers, next steps.
Peek at the other branch safely: `git fetch origin && git show origin/<their-branch>:docs/handoffs/person2-execution-plane.md`.

## Shared schema policy
Canonical contract = JSON Schemas in `packages/contracts/schemas/` (version in `packages/contracts/VERSION`, currently `0.1.0`) + fixtures in `packages/contracts/fixtures/`. Python (Pydantic) and the execution plane both validate against them. Never make an incompatible change without agreement from both people; document the proposal in your handoff.

## Testing
Run relevant tests before calling anything done. Use the project virtualenv, never a shared/global Python environment: `python3 -m venv .venv && .venv/bin/pip install -r apps/api/requirements-dev.txt && .venv/bin/python -m pytest -q`. Never report unverified functionality as working; label mocked/local-fallback pieces as such.

## Security requirements
- Never expose or log secrets, file contents, tokens, or full patches.
- Never bypass deterministic authorization; Bedrock never authorizes. Fail closed.
- Never silently expand sandbox/network/tool capabilities.
- Never modify the protected repository before approved, hash-verified promotion.

## Git safety
No force-push, reset, or other destructive Git operations without permission. Never silently overwrite another workstream's code. Small commits after tested milestones; push only to your own branch.

## Session completion
Update your handoff (status, verification, blockers, next steps, branch + commit hash), commit, and summarize.
