import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { parseReplayHash, promotionLabel, replayHref, reviewLabel, statusLabel, type Attempt, type ReplayView } from "../lib/replay";
import { ReplayDetail } from "./SecurityReplay";

afterEach(() => vi.unstubAllGlobals());

const SHA = "a".repeat(64);
const BASE = "b".repeat(40);
const PATCH = "diff --git a/src/auth/session.ts b/src/auth/session.ts\n--- a/src/auth/session.ts\n+++ b/src/auth/session.ts\n@@ -1 +1 @@\n-const timeout = 5;\n+const timeout = 30;\n";

function attempt(over: Partial<Attempt> = {}): Attempt {
  return {
    impact_report_id: "impact_1", created_at: "2026-11-08T01:06:00Z", sandbox_id: "sandbox_1", base_commit: BASE, patch_sha256: SHA,
    summary: { files_created: 0, files_modified: 1, files_deleted: 0, files_renamed: 0, lines_added: 1, lines_deleted: 1 },
    findings: [],
    authorization: { status: "allowed", basis: "propose_patch request event #3, the last before impact event #4.", seq: 3, request_id: "r-patch" },
    consequence: { status: "passed", decision: "pass", failing_tests: [], blocking_findings: [], tests_recorded: 1, basis: "Impact Preview report impact_1 (sandbox_1)." },
    review: { status: "none", basis: "No review decision was recorded." },
    promotion: { status: "none", basis: "No promotion was requested." },
    eligibility: { recorded: true, promotable: true, can_approve: true, can_promote: false, approve_blockers: [], promote_blockers: ["NOT_APPROVED"], basis: "recorded" },
    patch: { status: "verified", key: `runs/run_1/patches/${SHA}.patch`, sha256: SHA, base_commit: BASE, text: PATCH,
             files: [{ path: "src/auth/session.ts", status: "modified", added: 1, deleted: 1, withheld: false }],
             withheld_files: [], redactions: 0, truncated: false, bytes: PATCH.length, parsed: true },
    tests: [{ test_id: "auth-tests", status: "passed", exit_code: 0, duration_ms: 5500, output_key: "runs/run_1/tests/auth-tests_x.txt",
              output: "ok 1", output_status: "available", output_truncated: false }],
    ...over,
  };
}

function view(over: Partial<ReplayView> = {}): ReplayView {
  return {
    run_id: "run_1",
    evidence: { kind: "audit_export", source: "s3", source_label: "S3 bucket intentlock-evidence", events_available: true,
                audit: { key: "runs/run_1/audit/final.json", sha256: "c".repeat(64), bytes: 5729, source: "S3 bucket intentlock-evidence" } },
    run: { run_id: "run_1", contract_id: "contract_1", contract_version: 1, status: "completed", base_commit: BASE,
           created_at: "2026-11-08T01:05:00Z", started_at: "2026-11-08T01:05:00Z", stopped_at: null, completed_at: "2026-11-08T01:07:00Z", stop_reason: null },
    contract: { contract_id: "contract_1", version: 1, mission: "Fix authentication timeout bug", allowed_tools: ["read_file", "propose_patch"],
                allowed_paths: ["src/auth/**"], protected_paths: [".env*"], approved_tests: {}, created_by: "ali", approved_at: "2026-11-08T01:04:00Z", status: "active" },
    timeline: [
      { seq: null, timestamp: "2026-11-08T01:04:00Z", stage: "mission_approved", title: "Mission contract approved", status: "approved", source: "mission contract record", detail: "contract_1 v1 approved by ali" },
      { seq: 1, timestamp: "2026-11-08T01:05:00Z", stage: "run_started", title: "Run started", status: "recorded", source: "event #1" },
    ],
    authorization: { available: true, total: 1, allowed: 1, denied: 0, pending: 0, denied_requests: [] },
    attempts: [attempt()],
    outcome: { code: "awaiting_review", title: "Awaiting human review", tone: "review", detail: "passed Impact Preview" },
    claims: [], limitations: [],
    ...over,
  };
}

const blocked = () => attempt({
  consequence: { status: "blocked", decision: "block", failing_tests: ["auth-tests"], blocking_findings: ["CRITICAL_TEST_FAILURE"], tests_recorded: 1, basis: "Impact Preview report impact_1." },
  findings: [{ severity: "critical", code: "CRITICAL_TEST_FAILURE", message: "Approved authentication tests failed", evidence: "auth-tests" }],
  eligibility: { recorded: true, promotable: false, can_approve: false, can_promote: false, approve_blockers: ["IMPACT_BLOCKED"], promote_blockers: ["IMPACT_BLOCKED", "NOT_APPROVED"], basis: "recorded" },
  tests: [{ test_id: "auth-tests", status: "failed", exit_code: 1, duration_ms: 5500, output_key: null, output: null, output_status: "not_recorded", output_truncated: false }],
});

describe("Security Replay rendering", () => {
  it("shows that an authorized patch failed the consequence check and is never promotable", () => {
    const html = renderToStaticMarkup(<ReplayDetail v={view({ attempts: [blocked()], outcome: { code: "unsafe_patch_blocked", title: "Unsafe patch blocked", tone: "deny", detail: "x" } })} />);
    expect(html).toContain("Action authorization");
    expect(html).toContain("Consequence verification");
    expect(html).toContain("Allowed is not the same as safe");
    expect(html).toContain(">Allowed<");
    expect(html).toContain(">Blocked<");
    expect(html).toContain("Not promotable");
    expect(html).not.toContain(">Promoted<");
    expect(html).not.toContain("Eligible for review");
    expect(html).toContain("CRITICAL_TEST_FAILURE");
  });

  it("shows promotion only from a promotion record, not from approval", () => {
    const approvedOnly = attempt({ review: { status: "approved", basis: "Review record rev_1.", review_id: "rev_1", reviewer: "ali", reviewed_at: "2026-11-08T01:06:10Z", patch_sha256: SHA, base_commit: BASE, matches_patch: true } });
    const approvedHtml = renderToStaticMarkup(<ReplayDetail v={view({ attempts: [approvedOnly] })} />);
    expect(approvedHtml).toContain(">Approved<");
    expect(approvedHtml).toContain("Not promoted");
    expect(approvedHtml).not.toContain(">Promoted<");

    const promoted = attempt({ ...approvedOnly, promotion: { status: "promoted", basis: "Promotion record.", requested_by: "ali", applied_patch_sha256: SHA, applied_matches_approved: true, base_commit: BASE } });
    const html = renderToStaticMarkup(<ReplayDetail v={view({ attempts: [promoted], outcome: { code: "safe_patch_promoted", title: "Safe patch promoted", tone: "ok", detail: "x" } })} />);
    expect(html).toContain(">Promoted<");
    expect(html).toContain("identical to the approved patch");
    expect(html).toContain("runs/run_1/audit/final.json");
  });

  it("distinguishes missing evidence from success", () => {
    const missing = attempt({
      authorization: { status: "unavailable", basis: "Control-plane authorization events are not part of this evidence." },
      review: { status: "unavailable", basis: "Review and promotion records are not part of this evidence." },
      promotion: { status: "unavailable", basis: "Review and promotion records are not part of this evidence." },
      eligibility: { recorded: false, promotable: null, approve_blockers: [], promote_blockers: [], basis: "Review and promotion records are not part of this evidence." },
      consequence: { status: "passed", decision: "pass", failing_tests: [], blocking_findings: [], tests_recorded: 0, basis: "x" },
      tests: [], patch: { ...attempt().patch, status: "unavailable", text: null, reason: "ARTIFACT_UNAVAILABLE" },
    });
    const html = renderToStaticMarkup(<ReplayDetail v={view({
      attempts: [missing], run: null, contract: null, timeline: [],
      evidence: { kind: "impact_preview_artifacts", source: "s3", source_label: "S3", events_available: false, audit: null },
      authorization: { available: false, total: 0, allowed: 0, denied: 0, pending: 0, denied_requests: [] },
      limitations: ["No final audit export exists for this run in this evidence store."],
    })} />);
    expect(html).toContain("Evidence unavailable");
    expect(html).toContain("No trusted test result recorded");
    expect(html).toContain("This is missing evidence, not a passing result.");
    expect(html).toContain("Impact Preview artifacts only");
    expect(html).toContain("Partial timeline");
    expect(html).toContain("No final audit export exists");
    expect(html).not.toContain("all 0 passed");
  });

  it("renders denied requests for a run without a patch", () => {
    const html = renderToStaticMarkup(<ReplayDetail v={view({
      attempts: [],
      authorization: { available: true, total: 2, allowed: 1, denied: 1, pending: 0, denied_requests: [{ seq: 3, tool: "read_file", reason_code: "PROTECTED_PATH", request_id: "r2", arguments: { path: ".env" }, message: "The requested path is protected by the mission contract." }] },
      outcome: { code: "unauthorized_request_denied", title: "Unauthorized request denied", tone: "deny", detail: "1 tool request(s) denied" },
    })} />);
    expect(html).toContain("Unauthorized request denied");
    expect(html).toContain("PROTECTED_PATH");
    expect(html).toContain("No patch evaluated");
    expect(html).not.toContain("Patch inspection");
  });

  it("renders untrusted diff, message and output text as escaped text", () => {
    const evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    const a = attempt({
      patch: { ...attempt().patch, text: PATCH + `+${evil}\n`, files: [{ path: `src/${evil}.ts`, status: "modified", added: 1, deleted: 0, withheld: false }] },
      tests: [{ ...attempt().tests[0], output: evil }],
      findings: [{ severity: "high", code: "X", message: evil, evidence: evil }],
    });
    const html = renderToStaticMarkup(<ReplayDetail v={view({ attempts: [a], timeline: [{ seq: 1, timestamp: null, stage: "agent_note", title: "Agent activity", status: "recorded", source: "event #1", detail: evil }], limitations: [evil] })} />);
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;script&gt;alert(2)&lt;/script&gt;");
  });

  it("states withheld protected content, redactions and truncation", () => {
    const a = attempt({ patch: { ...attempt().patch, withheld_files: [".env.local"], redactions: 2, truncated: true,
      files: [...attempt().patch.files, { path: ".env.local", status: "modified", added: 1, deleted: 1, withheld: true }] } });
    const html = renderToStaticMarkup(<ReplayDetail v={view({ attempts: [a] })} />);
    expect(html).toContain("Protected content withheld");
    expect(html).toContain("2 credential-like values were redacted");
    expect(html).toContain("only the beginning is shown");
  });

  it("never displays a patch whose hash does not match", () => {
    const a = attempt({ patch: { ...attempt().patch, status: "hash_mismatch", text: null, reason: "Stored bytes do not hash to the reported digest." } });
    const html = renderToStaticMarkup(<ReplayDetail v={view({ attempts: [a] })} />);
    expect(html).toContain("Hash mismatch");
    expect(html).not.toContain("const timeout = 30");
  });
});

describe("Security Replay helpers and routing", () => {
  it("maps statuses to the standard labels", () => {
    expect(statusLabel("allowed")).toEqual(["Allowed", "allow"]);
    expect(statusLabel("denied")).toEqual(["Denied", "deny"]);
    expect(statusLabel("blocked")).toEqual(["Blocked", "deny"]);
    expect(statusLabel("approved")).toEqual(["Approved", "ok"]);
    expect(statusLabel("promoted")).toEqual(["Promoted", "ok"]);
    expect(statusLabel("unavailable")).toEqual(["Evidence unavailable", "na"]);
    expect(reviewLabel(attempt())).toEqual(["Pending review", "review"]);
    expect(reviewLabel(blocked())).toEqual(["Not eligible", "stopped"]);
    expect(promotionLabel(blocked())).toEqual(["Not promotable", "deny"]);
  });

  it("parses replay routes and rejects unsafe ids", () => {
    expect(parseReplayHash("#/replay")).toEqual({ runId: null, source: "auto" });
    expect(parseReplayHash("#/replay/run_ab12")).toEqual({ runId: "run_ab12", source: "auto" });
    expect(parseReplayHash("#/replay/run_ab12?source=s3")).toEqual({ runId: "run_ab12", source: "s3" });
    expect(parseReplayHash("#/replay/../etc")).toBeNull();
    expect(parseReplayHash("#/replay/run_1?source=file")).toBeNull();
    expect(parseReplayHash("#/runs/run_1/audit")).toBeNull();
    expect(replayHref("run_1", "local")).toBe("#/replay/run_1?source=local");
  });

  it("routes #/replay to the replay screen and keeps existing routes", () => {
    vi.stubGlobal("location", { hash: "#/replay/run_1" });
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("Security Replay");
    expect(html).toContain('href="#/replay"');
    vi.stubGlobal("location", { hash: "#/runs/run_123/contract" });
    expect(renderToStaticMarkup(<App />)).toContain("Loading the contract for this run");
  });
});
