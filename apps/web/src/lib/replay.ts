// Security Replay view returned by GET /api/replay/runs/{run_id}. Computed by the backend from stored evidence;
// the UI only renders it and never derives a security decision of its own.
export type Tone = "allow" | "deny" | "pending" | "stopped" | "review" | "info" | "ok" | "na";
export type EvidenceKind = "audit_export" | "impact_preview_artifacts" | "live_store";

export interface ReplaySource { id: string; label: string; available: boolean; reason: string | null; runs?: number }
export interface ReplayRunSummary {
  run_id: string; source: string; source_label: string; evidence: EvidenceKind; status: string | null;
  contract_id: string | null; contract_version: number | null; mission: string | null; attempts: number;
  outcome: string; outcome_title: string; last_activity: string | null;
}
export interface ReplayCatalog {
  sources: ReplaySource[]; runs: ReplayRunSummary[]; truncated: boolean;
  live_lookup: { store: string; persistent: boolean };
}

export interface TimelineItem {
  seq: number | null; timestamp: string | null; stage: string; title: string; status: string; source: string;
  detail?: string | null; tool?: string | null; reason_code?: string | null; request_id?: string | null;
  latency_ms?: number | null; arguments?: Record<string, unknown> | null; evidence?: Record<string, unknown> | null;
  impact_report_id?: string | null; reviewer?: string | null; patch_sha256?: string | null;
  tests?: { test_id: string; status: string; exit_code: number | null }[];
  findings?: { severity: string; code: string }[];
}
export interface DeniedRequest {
  seq: number; tool: string | null; reason_code: string | null; request_id: string | null;
  arguments: Record<string, unknown> | null; message: string | null;
}
export interface AuthorizationSummary {
  available: boolean; total: number; allowed: number; denied: number; pending: number; denied_requests: DeniedRequest[];
}
export interface PatchView {
  status: "verified" | "hash_mismatch" | "unavailable" | "key_invalid"; key: string; sha256: string; base_commit: string;
  text: string | null; files: { path: string; status: string; added: number; deleted: number; withheld: boolean }[];
  withheld_files: string[]; redactions: number; truncated: boolean; bytes: number | null; parsed: boolean; reason?: string;
}
export interface TestView {
  test_id: string; status: "passed" | "failed" | "timeout" | "error"; exit_code: number | null; duration_ms: number;
  output_key: string | null; output: string | null; output_status: "available" | "unavailable" | "not_recorded" | "key_invalid";
  output_truncated: boolean;
}
export interface Attempt {
  impact_report_id: string; created_at: string; sandbox_id: string; base_commit: string; patch_sha256: string;
  summary: { files_created: number; files_modified: number; files_deleted: number; files_renamed: number; lines_added: number; lines_deleted: number };
  findings: { severity: string; code: string; message: string; evidence: string | null }[];
  authorization: { status: string; basis: string; seq?: number; request_id?: string | null; reason_code?: string | null; latency_ms?: number | null };
  consequence: { status: "blocked" | "review_required" | "passed"; decision: string; failing_tests: string[]; blocking_findings: string[]; tests_recorded: number; basis: string };
  review: { status: "approved" | "rejected" | "none" | "unavailable"; basis: string; review_id?: string; reviewer?: string;
            reviewed_at?: string; comment?: string | null; patch_sha256?: string; base_commit?: string; contract_version?: number; matches_patch?: boolean };
  promotion: { status: "promoted" | "refused" | "failed" | "requested" | "inconsistent" | "none" | "unavailable"; basis: string;
               requested_by?: string; requested_at?: string; completed_at?: string | null; reason_code?: string | null; message?: string | null;
               review_id?: string; patch_sha256?: string; base_commit?: string; applied_patch_sha256?: string | null; applied_matches_approved?: boolean | null };
  eligibility: { recorded: boolean; promotable: boolean | null; can_approve?: boolean; can_promote?: boolean;
                 approve_blockers: string[]; promote_blockers: string[]; basis: string };
  patch: PatchView; tests: TestView[];
}
export interface ReplayView {
  run_id: string;
  evidence: { kind: EvidenceKind; source: string; source_label: string; events_available: boolean;
              audit: { key: string; sha256: string; bytes: number; source: string } | null };
  run: { run_id: string; contract_id: string; contract_version: number; status: string; base_commit: string; created_at: string;
         started_at: string | null; stopped_at: string | null; completed_at: string | null; stop_reason: string | null } | null;
  contract: { contract_id: string; version: number; mission: string; allowed_tools: string[]; allowed_paths: string[];
              protected_paths: string[]; approved_tests: Record<string, { argv: string[]; timeout_seconds: number }>;
              created_by: string; approved_at: string; status: string } | null;
  timeline: TimelineItem[]; authorization: AuthorizationSummary; attempts: Attempt[];
  outcome: { code: string; title: string; tone: Tone; detail: string; impact_report_id?: string | null };
  claims: { claim: string; basis: string }[]; limitations: string[];
}

const STATUS: Record<string, [string, Tone]> = {
  allowed: ["Allowed", "allow"], denied: ["Denied", "deny"], pending: ["Undecided", "pending"],
  blocked: ["Blocked", "deny"], passed: ["Passed", "ok"], review_required: ["Review required", "review"],
  approved: ["Approved", "ok"], rejected: ["Rejected", "stopped"], promoted: ["Promoted", "ok"],
  refused: ["Promotion refused", "deny"], failed: ["Failed", "deny"], requested: ["Promotion requested", "pending"],
  inconsistent: ["Integrity mismatch", "deny"], completed: ["Completed", "ok"], stopped: ["Stopped", "stopped"],
  active: ["Active", "allow"], recorded: ["Recorded", "info"],
  unavailable: ["Evidence unavailable", "na"], not_recorded: ["Not recorded", "na"],
};

export function statusLabel(status: string): [string, Tone] {
  return STATUS[status] ?? [status.replace(/_/g, " "), "info"];
}

/** Review status in context: "none" means pending review only when the backend recorded the patch as reviewable. */
export function reviewLabel(a: Attempt): [string, Tone] {
  if (a.review.status !== "none") return statusLabel(a.review.status);
  if (a.consequence.status === "blocked") return ["Not eligible", "stopped"];
  return ["Pending review", "review"];
}

export function promotionLabel(a: Attempt): [string, Tone] {
  if (a.promotion.status === "none") return a.consequence.status === "blocked" ? ["Not promotable", "deny"] : ["Not promoted", "stopped"];
  return statusLabel(a.promotion.status);
}

export const EVIDENCE_LABEL: Record<EvidenceKind, [string, Tone]> = {
  audit_export: ["Full audit export", "ok"],
  impact_preview_artifacts: ["Impact Preview artifacts only", "review"],
  live_store: ["Live control-plane store", "info"],
};

export const STAGE_LABEL: Record<string, string> = {
  mission_approved: "Mission", run_started: "Run", agent_started: "Agent", agent_note: "Agent",
  tool_request: "Authorization", impact_preview: "Impact Preview", review: "Review", promotion: "Promotion", run_final: "Final",
};

export const BLOCKER_TEXT: Record<string, string> = {
  IMPACT_BLOCKED: "Impact Preview blocked this patch", ALREADY_REVIEWED: "a review decision was already recorded",
  NOT_APPROVED: "no approval recorded", ALREADY_REQUESTED: "promotion was already requested",
  PATCH_HASH_MISMATCH: "patch bytes do not match the reported digest", PATCH_UNAVAILABLE: "patch artifact unavailable",
  STALE_BASE_COMMIT: "base commit no longer matches", CONTRACT_NOT_ACTIVE: "contract superseded or revoked",
  RUN_NOT_ELIGIBLE: "run was stopped, blocked or failed", RUN_NOT_FOUND: "run not found", CONTRACT_NOT_FOUND: "contract not found",
  PROMOTER_NOT_CONFIGURED: "no promoter configured", REVIEW_AUTH_NOT_CONFIGURED: "reviewer authentication not configured",
};
export const blockerText = (codes: string[]) => codes.map((c) => BLOCKER_TEXT[c] ?? c).join("; ");

/** Parses #/replay, #/replay/<run>, #/replay/<run>?source=<s3|local|control-plane>. */
export function parseReplayHash(hash: string): { runId: string | null; source: string } | null {
  const m = /^#\/replay(?:\/([A-Za-z0-9_-]{1,100}))?(?:\?source=(auto|s3|local|control-plane))?$/.exec(hash);
  if (!m) return null;
  return { runId: m[1] ?? null, source: m[2] ?? "auto" };
}

export const replayHref = (runId: string, source?: string) =>
  `#/replay/${encodeURIComponent(runId)}${source && source !== "auto" ? `?source=${source}` : ""}`;
