import type { EventRec } from "./types";

/** Merge by seq: a later copy of the same seq replaces the earlier (pending -> decided). */
export function mergeEvents(prev: EventRec[], incoming: EventRec[]): EventRec[] {
  if (incoming.length === 0) return prev;
  const bySeq = new Map<number, EventRec>(prev.map((e) => [e.seq, e]));
  for (const e of incoming) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

export function toolEvents(events: EventRec[]): EventRec[] {
  return events.filter((e) => e.event_type === "tool_request");
}

export function countDecisions(events: EventRec[]) {
  const t = toolEvents(events);
  return {
    allowed: t.filter((e) => e.decision === "allow").length,
    denied: t.filter((e) => e.decision === "deny").length,
    pending: t.filter((e) => e.decision === null).length,
  };
}

/** Nearest-rank percentile over REAL backend-measured policy latencies (ms). */
export function latencyStats(events: EventRec[]) {
  const xs = toolEvents(events).map((e) => e.latency_ms).filter((x): x is number => x !== null).sort((a, b) => a - b);
  if (xs.length === 0) return null;
  const pct = (p: number) => xs[Math.min(xs.length - 1, Math.ceil((p / 100) * xs.length) - 1)];
  return { count: xs.length, min: xs[0], p50: pct(50), p95: pct(95), max: xs[xs.length - 1] };
}

/** Latest impact_report id the execution plane reported via /internal/events details. */
export function findImpactReportId(events: EventRec[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.event_type === "impact_report") {
      const id = e.details?.["impact_report_id"];
      if (typeof id === "string" && id) return id;
    }
  }
  return null;
}

/** All report ids in newest-first order so a reviewer can inspect earlier blocked attempts. */
export function impactReportIds(events: EventRec[]): string[] {
  const ids: string[] = [];
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    const id = e.event_type === "impact_report" ? e.details?.["impact_report_id"] : null;
    if (typeof id === "string" && id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}
