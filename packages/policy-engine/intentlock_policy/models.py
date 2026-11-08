"""Pydantic models for shared schema version 0.1.0.

The committed JSON Schemas in packages/contracts/schemas/ are the canonical
cross-language contract; these models must stay in sync with them (enforced by
tests/contract). Do not make breaking changes without agreement from Person 2.
"""
from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .paths import PathError, validate_pattern

SCHEMA_VERSION = "0.1.0"

ToolName = Literal[
    "list_files",
    "read_file",
    "search_code",
    "propose_patch",
    "run_approved_test",
    "run_approved_command",
    "request_permission",
    "finish_task",
]
TOOL_NAMES: tuple[str, ...] = get_args(ToolName)

ReasonCode = Literal[
    "ALLOW",
    "CONTRACT_NOT_FOUND",
    "CONTRACT_VERSION_MISMATCH",
    "CONTRACT_NOT_ACTIVE",
    "RUN_NOT_ACTIVE",
    "TOOL_NOT_ALLOWED",
    "INVALID_ARGUMENTS",
    "PATH_NOT_ALLOWED",
    "PROTECTED_PATH",
    "TEST_NOT_APPROVED",
    "NETWORK_NOT_ALLOWED",
    "TOOL_CALL_LIMIT_EXCEEDED",
    "POLICY_ENGINE_ERROR",
    "IMPACT_BLOCKED",
    "STALE_BASE_COMMIT",
    "PATCH_HASH_MISMATCH",
]

GIT_SHA = r"^[0-9a-f]{40}$"
SHA256 = r"^[0-9a-f]{64}$"
TEST_ID = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovedTest(_Strict):
    argv: list[str] = Field(min_length=1, max_length=32)
    timeout_seconds: int = Field(gt=0, le=600)

    @field_validator("argv")
    @classmethod
    def _argv_items(cls, v: list[str]) -> list[str]:
        if any((not a) or "\x00" in a for a in v):
            raise ValueError("argv items must be non-empty strings without NUL")
        return v


class Limits(_Strict):
    max_files_changed: int = Field(ge=0, le=1000)
    max_files_deleted: int = Field(ge=0, le=1000)
    max_lines_added: int = Field(ge=0, le=100000)
    max_lines_deleted: int = Field(ge=0, le=100000)
    max_tool_calls: int = Field(ge=1, le=10000)
    run_timeout_seconds: int = Field(gt=0, le=600)


class MissionContractDraft(_Strict):
    """What Bedrock proposes and the human edits. Never enforceable by itself."""

    mission: str = Field(min_length=1, max_length=2000)
    repository_id: str = Field(min_length=1, max_length=128)
    base_commit: str = Field(pattern=GIT_SHA)
    allowed_tools: list[ToolName] = Field(min_length=1)
    allowed_paths: list[str] = Field(min_length=1, max_length=64)
    protected_paths: list[str] = Field(max_length=64)
    approved_tests: dict[str, ApprovedTest]
    # Hackathon scope freeze: network is never grantable.
    network_access: Literal[False] = False
    limits: Limits
    required_exports: list[str] = Field(max_length=64)

    @field_validator("allowed_paths", "protected_paths")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            try:
                out.append(validate_pattern(p))
            except PathError as e:
                raise ValueError(f"invalid path pattern {p!r}: {e.detail}") from e
        return out

    @field_validator("allowed_tools")
    @classmethod
    def _unique_tools(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("allowed_tools must be unique")
        return v

    @field_validator("approved_tests")
    @classmethod
    def _test_ids(cls, v: dict[str, ApprovedTest]) -> dict[str, ApprovedTest]:
        import re

        for k in v:
            if not re.match(TEST_ID, k):
                raise ValueError(f"invalid test id {k!r}")
        return v


class MissionContract(MissionContractDraft):
    contract_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    created_by: str = Field(min_length=1, max_length=128)
    approved_at: AwareDatetime
    status: Literal["active", "superseded", "revoked"]


class ToolRequest(_Strict):
    request_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: int = Field(ge=1)
    # Not a Literal: unknown tools must reach the policy and be denied
    # with TOOL_NOT_ALLOWED rather than failing envelope validation.
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]
    agent_id: str = Field(min_length=1, max_length=128)
    timestamp: AwareDatetime


class AuthorizationDecision(_Strict):
    request_id: str
    decision: Literal["allow", "deny"]
    reason_code: ReasonCode
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    stop_run: bool = False
    latency_ms: float = Field(ge=0)


RunStatus = Literal["created", "active", "stopped", "blocked", "completed", "failed"]


class Run(_Strict):
    run_id: str
    contract_id: str
    contract_version: int = Field(ge=1)
    status: RunStatus
    agent_process_id: str | None = None
    session_id: str | None = None
    sandbox_id: str | None = None
    base_commit: str = Field(pattern=GIT_SHA)
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    stopped_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    stop_reason: str | None = None


EventType = Literal[
    "run_started",
    "tool_request",
    "run_stopped",
    "agent_event",
    "impact_report",
    "review_decision",
    "promotion",
    "run_completed",
]


class Event(_Strict):
    """One timeline entry. A tool_request event is created `pending` before
    authorization and updated to `decided` with the decision (PRD FR-4)."""

    event_id: str
    run_id: str
    seq: int = Field(ge=1)
    request_id: str | None = None
    event_type: EventType
    timestamp: AwareDatetime
    status: Literal["pending", "decided", "recorded"]
    tool: str | None = None
    # Redacted arguments only. Never file contents or full patches.
    arguments_redacted: dict[str, Any] | None = None
    decision: Literal["allow", "deny"] | None = None
    reason_code: ReasonCode | None = None
    message: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    evidence: dict[str, Any] | None = None
    details: dict[str, Any] | None = None


class ImpactTest(_Strict):
    test_id: str
    status: Literal["passed", "failed", "timeout", "error"]
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    output_s3_key: str | None = None
    # Additive (0.1.0-compatible): why Impact Preview selected this catalogue check for this patch.
    reason: str | None = None


class ImpactCoverage(_Strict):
    """Additive: which trusted checks were selected from the changed paths, and what no test covers."""

    selected_checks: list[str]
    uncovered_paths: list[str]
    not_approved_checks: list[str] = Field(default_factory=list)


class ImpactFinding(_Strict):
    severity: Literal["critical", "high", "medium", "low", "info"]
    code: str
    message: str
    evidence: str | None = None


class ImpactSummary(_Strict):
    files_created: int = Field(ge=0)
    files_modified: int = Field(ge=0)
    files_deleted: int = Field(ge=0)
    files_renamed: int = Field(ge=0)
    lines_added: int = Field(ge=0)
    lines_deleted: int = Field(ge=0)


class ImpactReport(_Strict):
    impact_report_id: str
    run_id: str
    sandbox_id: str
    contract_id: str
    contract_version: int = Field(ge=1)
    base_commit: str = Field(pattern=GIT_SHA)
    patch_s3_key: str
    patch_sha256: str = Field(pattern=SHA256)
    summary: ImpactSummary
    tests: list[ImpactTest]
    findings: list[ImpactFinding]
    # "pass" = may proceed to human review; it never means auto-apply.
    decision: Literal["block", "review_required", "pass"]
    coverage: ImpactCoverage | None = None
    created_at: AwareDatetime


class ReviewApproval(_Strict):
    review_id: str
    impact_report_id: str
    run_id: str
    contract_id: str
    contract_version: int = Field(ge=1)
    base_commit: str = Field(pattern=GIT_SHA)
    patch_sha256: str = Field(pattern=SHA256)
    decision: Literal["approve", "reject"]
    reviewer: str
    reviewed_at: AwareDatetime
    comment: str | None = None


SHARED_MODELS: dict[str, type[BaseModel]] = {
    "mission-contract": MissionContract,
    "mission-contract-draft": MissionContractDraft,
    "tool-request": ToolRequest,
    "authorization-decision": AuthorizationDecision,
    "run": Run,
    "event": Event,
    "impact-report": ImpactReport,
    "review-approval": ReviewApproval,
}
