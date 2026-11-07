import type { Contract, ContractDraft, EventRec, Health, ImpactReport, ImpactStatus, LauncherInfo, LaunchStatus, Run } from "./types";

// Empty in dev => same-origin via the Vite proxy. Set VITE_API_URL for a deployed API.
export const API_BASE: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`${status || "NETWORK"}: ${detail}`);
  }
  /** Route missing / not wired up yet / upstream down: treat as "unavailable", not as data. */
  get unavailable(): boolean { return [0, 404, 501, 502, 503].includes(this.status); }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? e.message : "network error");
  }
  const text = await res.text();
  let body: unknown = text;
  try { body = text ? JSON.parse(text) : null; } catch { /* keep text */ }
  if (!res.ok) throw new ApiError(res.status, describeError(body));
  return body as T;
}

/** FastAPI errors: {detail: "CODE"} or {detail: [{loc,msg},…]} (422). */
export function describeError(body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d.map((x) => {
        const o = x as { loc?: unknown[]; msg?: string };
        return `${(o.loc ?? []).filter((p) => p !== "body").join(".")}: ${o.msg ?? "invalid"}`;
      }).join("; ");
    }
  }
  return typeof body === "string" && body ? body.slice(0, 200) : "request failed";
}

const post = (b: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(b) });

export const api = {
  health: () => call<Health>("/health"),
  propose: (b: { task: string; repository_id: string; base_commit: string }) =>
    call<{ draft: ContractDraft; attempts: number; status: string; requires_human_approval: boolean }>(
      "/api/contracts/propose", post(b)),
  createContract: (b: { draft: ContractDraft; approved: boolean; approved_by: string; contract_id?: string }) =>
    call<Contract>("/api/contracts", post(b)),
  getContract: (id: string, v: number) => call<Contract>(`/api/contracts/${encodeURIComponent(id)}/versions/${v}`),
  createRun: (b: { contract_id: string; contract_version: number }) => call<Run>("/api/runs", post(b)),
  getRun: (id: string) => call<Run>(`/api/runs/${encodeURIComponent(id)}`),
  stopRun: (id: string, reason: string) =>
    call<{ run: Run; state_committed: boolean; process_termination: string }>(
      `/api/runs/${encodeURIComponent(id)}/stop`, post({ reason })),
  getEvents: (id: string, afterSeq = 0) =>
    call<{ events: EventRec[] }>(`/api/runs/${encodeURIComponent(id)}/events?after_seq=${afterSeq}`),
  streamUrl: (id: string, afterSeq = 0) =>
    `${API_BASE}/api/runs/${encodeURIComponent(id)}/events/stream?after_seq=${afterSeq}`,

  launcherInfo: () => call<LauncherInfo>("/api/launcher"),
  launchAgent: (id: string) => call<{ launched: boolean; launcher_pid: number }>(
    `/api/runs/${encodeURIComponent(id)}/launch`, { method: "POST" }),
  launchStatus: (id: string) => call<LaunchStatus>(`/api/runs/${encodeURIComponent(id)}/launch`),

  // --- Execution-plane / TwinGuard interfaces (NOT implemented by this backend yet). ---
  // Expected shapes are documented in docs/handoffs/person1-control-plane.md.
  getImpact: (id: string) => call<ImpactReport>(`/api/impact/${encodeURIComponent(id)}`),
  impactStatus: (id: string) => call<ImpactStatus>(`/api/impact/${encodeURIComponent(id)}/status`),
  getPatch: async (id: string): Promise<string> => {
    let res: Response;
    try { res = await fetch(`${API_BASE}/api/impact/${encodeURIComponent(id)}/patch`); }
    catch (e) { throw new ApiError(0, e instanceof Error ? e.message : "network error"); }
    if (!res.ok) throw new ApiError(res.status, "patch unavailable");
    return res.text();
  },
  review: (id: string, action: "approve" | "reject",
           b: { reviewer: string; patch_sha256: string; base_commit: string }) =>
    call<Record<string, unknown>>(`/api/impact/${encodeURIComponent(id)}/${action}`, post(b)),
  promote: (id: string, b: { requested_by: string; patch_sha256: string }) =>
    call<Record<string, unknown>>(`/api/impact/${encodeURIComponent(id)}/promote`, post(b)),
};
