"""Amazon Bedrock contract proposal. Bedrock PROPOSES; it never authorizes.

UNVERIFIED against real AWS until tested in an account with model access
(see handoff). Output is strictly validated with Pydantic; invalid output is
retried once and otherwise rejected, so it can never become a contract.
"""
import json
import os
import re
from typing import Callable

from pydantic import ValidationError

from intentlock_policy.models import MissionContractDraft

from .store import aws_region

SYSTEM_PROMPT = """You draft least-privilege mission contracts for a coding agent.
Return ONLY one JSON object (no prose, no code fences) with exactly these keys:
mission (string), allowed_tools (subset of list_files, read_file, search_code,
propose_patch, run_approved_test), allowed_paths (repo-relative globs),
protected_paths (globs; always include ".env*", ".git/**", ".github/**", "infra/**"),
approved_tests (for this TypeScript/Node demo, exactly one entry named "auth-tests"
with argv ["npm","run","test:auth"] and timeout_seconds 60),
limits ({max_files_changed, max_files_deleted, max_lines_added, max_lines_deleted,
max_tool_calls, run_timeout_seconds<=600}), required_exports (include "verifyToken"),
network_access (always false). Be as narrow as the task allows."""


class ProposalError(Exception):
    pass


def _extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


def propose_draft(task: str, repository_id: str, base_commit: str,
                  invoke: Callable[[str, str], str]) -> tuple[MissionContractDraft, int]:
    """invoke(system, user) -> raw model text. Returns (draft, attempts)."""
    user = f"Task: {task}"
    last = ""
    for attempt in (1, 2):
        raw = invoke(SYSTEM_PROMPT, user if attempt == 1 else f"{user}\n\nYour previous output was invalid: {last}. Return valid JSON only.")
        try:
            obj = _extract_json(raw)
            # Never trust the model for repository identity or network access.
            obj["repository_id"], obj["base_commit"], obj["network_access"] = repository_id, base_commit, False
            draft = MissionContractDraft.model_validate(obj)
            tests = draft.approved_tests
            if set(tests) != {"auth-tests"} or tests["auth-tests"].argv != ["npm", "run", "test:auth"]:
                raise ValueError("approved_tests must contain only auth-tests with npm run test:auth")
            if "verifyToken" not in draft.required_exports:
                raise ValueError("required_exports must include verifyToken")
            return draft, attempt
        except (ValueError, ValidationError) as e:
            last = str(e)[:300]
    raise ProposalError("Bedrock output was not a valid mission contract after 2 attempts")


def bedrock_invoker() -> Callable[[str, str], str]:
    """Real Bedrock Converse call. Requires AWS credentials + model access."""
    import boto3  # imported lazily so local dev/tests don't need it

    model_id = os.environ.get("INTENTLOCK_BEDROCK_MODEL_ID")
    if not model_id:
        raise ProposalError("INTENTLOCK_BEDROCK_MODEL_ID is not set")
    client = boto3.client("bedrock-runtime", region_name=aws_region())

    def invoke(system: str, user: str) -> str:
        resp = client.converse(modelId=model_id, system=[{"text": system}],
                               messages=[{"role": "user", "content": [{"text": user}]}],
                               inferenceConfig={"maxTokens": 1500})
        # Some models emit reasoningContent before the user-visible text block.
        for block in resp["output"]["message"]["content"]:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]
        raise ProposalError("Bedrock returned no text proposal")

    return invoke
