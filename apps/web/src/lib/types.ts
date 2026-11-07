// Mirrors shared schema 0.1.0 (packages/contracts/schemas). Keep in sync; do not extend unilaterally.
export type RunStatus = "created" | "active" | "stopped" | "blocked" | "completed" | "failed";
export const TERMINAL: RunStatus[] = ["stopped", "blocked", "completed", "failed"];

export interface Run {
  run_id: string;
  contract_id: string;
  contract_version: number;
  status: RunStatus;
  agent_process_id: string | null;
  session_id: string | null;
  sandbox_id: string | null;
  base_commit: string;
  created_at: string;
  started_at: string | null;
  stopped_at: string | null;
  completed_at: string | null;
  stop_reason: string | null;
}

export interface ApprovedTest { argv: string[]; timeout_seconds: number }
export interface Limits {
  max_files_changed: number; max_files_deleted: number;
  max_lines_added: number; max_lines_deleted: number;
  max_tool_calls: number; run_timeout_seconds: number;
}
export type ToolName = "list_files" | "read_file" | "search_code" | "propose_patch" | "run_approved_test";
export interface ContractDraft {
  mission: string; repository_id: string; base_commit: string;
  allowed_tools: string[]; allowed_paths: string[]; protected_paths: string[];
  approved_tests: Record<string, ApprovedTest>;
  network_access: false; limits: Limits; required_exports: string[];
}
export interface Contract extends ContractDraft {
  contract_id: string; version: number; created_by: string; approved_at: string;
  status: "active" | "superseded" | "revoked";
}

export type EventType =
  | "run_started" | "tool_request" | "run_stopped" | "agent_event"
  | "impact_report" | "review_decision" | "promotion" | "run_completed";
export const EVENT_TYPES: EventType[] = [
  "run_started", "tool_request", "run_stopped", "agent_event",
  "impact_report", "review_decision", "promotion", "run_completed",
];
export interface EventRec {
  event_id: string; run_id: string; seq: number; request_id: string | null;
  event_type: EventType; timestamp: string; status: "pending" | "decided" | "recorded";
  tool: string | null; arguments_redacted: Record<string, unknown> | null;
  decision: "allow" | "deny" | null; reason_code: string | null; message: string | null;
  latency_ms: number | null; evidence: Record<string, unknown> | null;
  details: Record<string, unknown> | null;
}

export interface ImpactReport {
  impact_report_id: string; run_id: string; sandbox_id: string; contract_id: string;
  contract_version: number; base_commit: string; patch_s3_key: string; patch_sha256: string;
  summary: { files_created: number; files_modified: number; files_deleted: number;
             files_renamed: number; lines_added: number; lines_deleted: number };
  tests: { test_id: string; status: "passed" | "failed" | "timeout" | "error";
           exit_code: number | null; duration_ms: number; output_s3_key: string | null }[];
  findings: { severity: "critical" | "high" | "medium" | "low" | "info"; code: string;
              message: string; evidence: string | null }[];
  decision: "block" | "review_required" | "pass";
  created_at: string;
}

export interface Health { status: string; schema_version: string; store: string }
