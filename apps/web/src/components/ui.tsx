import type { ReactNode } from "react";
import type { RunStatus } from "../lib/types";

export function Badge({ tone, children }: { tone: "allow" | "deny" | "pending" | "stopped" | "review" | "info" | "ok" | "na"; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function StatusPill({ status }: { status: RunStatus }) {
  const tone = status === "active" ? "allow" : status === "blocked" ? "deny"
    : status === "completed" ? "ok" : status === "created" ? "pending" : "stopped";
  return <Badge tone={tone}>{status.toUpperCase()}</Badge>;
}

export function Notice({ tone, title, children }: { tone: "error" | "warn" | "info"; title?: string; children?: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}>
      {title && <strong>{title}</strong>}
      {children && <div>{children}</div>}
    </div>
  );
}

export function Card({ title, right, children }: { title?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || right) && <header className="card-h"><h2>{title}</h2><div>{right}</div></header>}
      {children}
    </section>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Spinner({ label }: { label: string }) {
  return <div className="spinner" role="status"><span className="dot" />{label}</div>;
}

const STEPS = ["Mission", "Contract", "Agent activity", "Impact Preview", "Audit"] as const;
export function Stepper({ active, runId }: { active: 0 | 1 | 2 | 3 | 4; runId?: string }) {
  const runPath = runId ? `#/runs/${encodeURIComponent(runId)}` : undefined;
  const hrefs = ["#/", runPath ? `${runPath}/contract` : "#/review", runPath,
    runPath ? `${runPath}/impact` : undefined, runPath ? `${runPath}/audit` : undefined];
  return (
    <ol className="stepper" aria-label="Workflow">
      {STEPS.map((s, i) => (
        <li key={s} className={i === active ? "on" : i < active ? "done" : ""}>
          {hrefs[i] ? <a href={hrefs[i]}>{i + 1}. {s}</a> : <span>{i + 1}. {s}</span>}
        </li>
      ))}
    </ol>
  );
}
