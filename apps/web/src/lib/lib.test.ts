import { describe, expect, it } from "vitest";
import { countDecisions, findImpactReportId, latencyStats, mergeEvents } from "./events";
import { parseUnifiedDiff } from "./diff";
import { describeError } from "./api";
import { elapsedSeconds, lines } from "./format";
import type { EventRec } from "./types";

const ev = (seq: number, over: Partial<EventRec> = {}): EventRec => ({
  event_id: `e${seq}`, run_id: "r", seq, request_id: null, event_type: "tool_request",
  timestamp: "2026-11-07T10:00:00Z", status: "decided", tool: "read_file", arguments_redacted: null,
  decision: "allow", reason_code: "ALLOW", message: null, latency_ms: seq, evidence: null, details: null, ...over,
});

describe("events", () => {
  it("merges by seq, replaces updates, sorts", () => {
    const a = mergeEvents([], [ev(2), ev(1)]);
    expect(a.map((e) => e.seq)).toEqual([1, 2]);
    const b = mergeEvents(a, [ev(2, { decision: "deny" }), ev(3)]);
    expect(b.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(b[1].decision).toBe("deny");
    expect(mergeEvents(b, [])).toBe(b);
  });
  it("counts decisions from tool_request events only", () => {
    const c = countDecisions([ev(1), ev(2, { decision: "deny" }), ev(3, { decision: null, status: "pending" }),
      ev(4, { event_type: "run_started", decision: null })]);
    expect(c).toEqual({ allowed: 1, denied: 1, pending: 1 });
  });
  it("computes nearest-rank latency stats and null when empty", () => {
    expect(latencyStats([])).toBeNull();
    const s = latencyStats([1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => ev(i)))!;
    expect(s).toMatchObject({ count: 10, min: 1, p50: 5, p95: 10, max: 10 });
  });
  it("finds the latest impact_report id", () => {
    expect(findImpactReportId([ev(1)])).toBeNull();
    const id = findImpactReportId([
      ev(1, { event_type: "impact_report", details: { impact_report_id: "a" } }),
      ev(2, { event_type: "impact_report", details: { impact_report_id: "b" } }),
      ev(3, { event_type: "agent_event" })]);
    expect(id).toBe("b");
  });
});

describe("diff parser", () => {
  const patch = [
    "diff --git a/src/auth/a.ts b/src/auth/a.ts", "--- a/src/auth/a.ts", "+++ b/src/auth/a.ts",
    "@@ -1,2 +1,2 @@", " keep", "-old", "+new",
    "diff --git a/n.ts b/n.ts", "new file mode 100644", "--- /dev/null", "+++ b/n.ts", "@@ -0,0 +1 @@", "+x",
  ].join("\n");
  it("parses files, counts and line numbers", () => {
    const f = parseUnifiedDiff(patch);
    expect(f.map((x) => [x.path, x.status, x.added, x.deleted])).toEqual([
      ["src/auth/a.ts", "modified", 1, 1], ["n.ts", "created", 1, 0]]);
    expect(f[0].lines.find((l) => l.kind === "del")).toMatchObject({ oldNo: 2, newNo: null, text: "old" });
    expect(f[0].lines.find((l) => l.kind === "add")).toMatchObject({ oldNo: null, newNo: 2, text: "new" });
  });
  it("returns [] for garbage", () => expect(parseUnifiedDiff("hello")).toEqual([]));
});

describe("helpers", () => {
  it("describes FastAPI errors", () => {
    expect(describeError({ detail: "APPROVAL_REQUIRED" })).toBe("APPROVAL_REQUIRED");
    expect(describeError({ detail: [{ loc: ["body", "draft", "network_access"], msg: "bad" }] })).toBe("draft.network_access: bad");
    expect(describeError(null)).toBe("request failed");
  });
  it("parses lines and elapsed", () => {
    expect(lines(" a \n\n b\n")).toEqual(["a", "b"]);
    expect(elapsedSeconds("2026-11-07T10:00:00Z", "2026-11-07T10:00:42Z", 0)).toBe(42);
    expect(elapsedSeconds(null, null, 0)).toBeNull();
  });
});

describe("review API client", () => {
  it("sends exactly one JSON content-type plus the reviewer token (regression: duplicate Content-Type caused 422)", async () => {
    const { api } = await import("./api");
    const seen: RequestInit[] = [];
    const orig = globalThis.fetch;
    globalThis.fetch = (async (_u: RequestInfo | URL, init?: RequestInit) => { seen.push(init ?? {}); return new Response("{}", { status: 200 }); }) as typeof fetch;
    try {
      await api.review("i1", "approve", { reviewer: "r", patch_sha256: "a".repeat(64), base_commit: "b".repeat(40) }, "tok");
      await api.promote("i1", { requested_by: "r", patch_sha256: "a".repeat(64) }, "tok");
    } finally { globalThis.fetch = orig; }
    for (const init of seen) {
      const h = new Headers(init.headers as HeadersInit);
      expect(h.get("content-type")).toBe("application/json");            // not "application/json, application/json"
      expect(h.get("x-intentlock-reviewer-token")).toBe("tok");
      expect(JSON.parse(init.body as string)).toBeTypeOf("object");
    }
  });
});
