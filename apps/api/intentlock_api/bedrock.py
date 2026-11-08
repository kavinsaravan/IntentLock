"""Amazon Bedrock contract proposal. Bedrock PROPOSES; it never authorizes.

UNVERIFIED against real AWS until tested in an account with model access
(see handoff). Output is strictly validated with Pydantic; invalid output is
retried once and otherwise rejected, so it can never become a contract.
"""
import json
import os
import re
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from intentlock_policy.models import MissionContractDraft

from .store import aws_region

# Trusted check catalogue (administrator-owned). Bedrock may only pick ids from it; the argv/timeouts are
# copied from the catalogue by validation, never invented by the model. If the file is not packaged with
# this deployment, fall back to the original frozen rule (auth-tests only).
CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "packages" / "impact-analyzer" / "catalogue.json"
FROZEN_CHECKS = {"auth-tests": {"argv": ["npm", "run", "test:auth"], "timeout_seconds": 60}}

_PROMPT = """You draft least-privilege mission contracts for a coding agent.
Return ONLY one JSON object (no prose, no code fences) with exactly these keys:
mission (string), allowed_tools (subset of list_files, read_file, search_code,
propose_patch, run_approved_test, run_approved_command), allowed_paths (repo-relative globs),
protected_paths (globs; always include ".env*", ".git/**", ".github/**", "infra/**"),
approved_tests (an object keyed by check id; choose ONLY from these trusted checks, copy argv
exactly, and choose a positive timeout_seconds no greater than the listed maximum: {checks}. Pick the checks relevant to the task; Impact Preview
selects which of them to run from the paths the patch actually changes),
limits ({{max_files_changed, max_files_deleted, max_lines_added, max_lines_deleted,
max_tool_calls, run_timeout_seconds<=600}}), required_exports (include "verifyToken"),
network_access (always false). Be as narrow as the task allows."""


def trusted_checks() -> dict[str, dict]:
    try:
        checks = json.loads(CATALOGUE_PATH.read_text())["checks"]
        return {cid: {"argv": c["argv"], "timeout_seconds": c["timeout_seconds"]} for cid, c in checks.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return dict(FROZEN_CHECKS)


def system_prompt() -> str:
    return _PROMPT.format(checks=json.dumps(trusted_checks(), separators=(",", ":")))


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
        raw = invoke(system_prompt(), user if attempt == 1 else f"{user}\n\nYour previous output was invalid: {last}. Return valid JSON only.")
        try:
            obj = _extract_json(raw)
            # Never trust the model for repository identity or network access.
            obj["repository_id"], obj["base_commit"], obj["network_access"] = repository_id, base_commit, False
            draft = MissionContractDraft.model_validate(obj)
            tests, trusted = draft.approved_tests, trusted_checks()
            if not tests or any(cid not in trusted or t.argv != trusted[cid]["argv"] or t.timeout_seconds > trusted[cid]["timeout_seconds"]
                                for cid, t in tests.items()):
                raise ValueError("approved_tests must be a non-empty subset of the trusted check catalogue with exact argv and a bounded timeout")
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
