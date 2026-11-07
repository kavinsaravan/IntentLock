import { useEffect, useState } from "react";
import { Badge, Card, Json, Notice, Spinner, StatusPill, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { findImpactReportId, latencyStats, toolEvents } from "../lib/events";
import { fmtMs, fmtTime, shortSha } from "../lib/format";
import type { Contract, ImpactReport } from "../lib/types";
import { useRunLive } from "../lib/useRunLive";

export function FinalAudit({ runId }: { runId: string }) {
  const { run, events, notFound, error } = useRunLive(runId);
  const [contract, setContract] = useState<Contract | null>(null);
  const [impact, setImpact] = useState<ImpactReport | null>(null);
  const impactId = findImpactReportId(events);

  useEffect(() => {
    if (run) api.getContract(run.contract_id, run.contract_version).then(setContract).catch(() => undefined);
  }, [run?.contract_id, run?.contract_version]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (impactId) api.getImpact(impactId).then(setImpact).catch((e) => { if (!(e instanceof ApiError)) throw e; });
  }, [impactId]);

  if (notFound) return (<><Stepper active={4} /><Notice tone="error" title="Run not found">No run <code>{runId}</code> on this backend.</Notice></>);
  if (!run) return (<><Stepper active={4} runId={runId} /><Spinner label="Loading audit data from backend…" /></>);

  const tools = toolEvents(events);
  const stops = events.filter((e) => e.event_type === "run_stopped");
  const exec = events.filter((e) => ["impact_report", "review_decision", "promotion", "run_completed", "agent_event"].includes(e.event_type));
  const promotions = events.filter((e) => e.event_type === "promotion");
  const stats = latencyStats(events);

  function exportJson() {
    const blob = new Blob([JSON.stringify({ run, contract, events, impact_report: impact }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `intentlock-audit-${runId}.json`; a.click(); URL.revokeObjectURL(a.href);
  }

  return (
    <>
      <Stepper active={4} runId={runId} />
      {error && <Notice tone="warn" title="Backend error">{error}. Showing last known data.</Notice>}
      <Card title="Outcome" right={<><StatusPill status={run.status} /> <button className="btn" onClick={exportJson}>Export JSON</button></>}>
        <dl className="kv">
          <dt>Run</dt><dd><code>{run.run_id}</code></dd>
          <dt>Started / ended</dt><dd>{run.started_at ?? "—"} → {run.stopped_at ?? run.completed_at ?? "still active"}</dd>
          {run.stop_reason && (<><dt>Stop reason</dt><dd><code>{run.stop_reason}</code></dd></>)}
          <dt>Base commit</dt><dd><code>{shortSha(run.base_commit, 12)}</code></dd>
          <dt>Promotion</dt><dd>{promotions.length ? promotions.map((p) => <div key={p.seq}><Badge tone="ok">{p.event_type}</Badge> {p.message ?? ""} <Json value={p.details} /></div>)
            : <span className="muted">No promotion event recorded.</span>}</dd>
        </dl>
      </Card>
      <Card title="Mission and contract">
        {!contract ? <p className="muted">Contract not loaded.</p> : (<>
          <p>{contract.mission}</p>
          <p className="muted"><code>{contract.contract_id}</code> v{contract.version} · {contract.status} · approved by {contract.created_by} at {contract.approved_at}</p>
          <details><summary>Full contract</summary><Json value={contract} /></details></>)}
      </Card>
      <Card title={`Authorization decisions (${tools.length})`}>
        {tools.length === 0 ? <p className="muted">No tool requests recorded.</p> : (
          <table className="tbl"><thead><tr><th>#</th><th>Time</th><th>Tool</th><th>Decision</th><th>Reason</th><th>Latency</th></tr></thead><tbody>
            {tools.map((e) => <tr key={e.seq}><td>{e.seq}</td><td>{fmtTime(e.timestamp)}</td><td><code>{e.tool}</code></td>
              <td><Badge tone={e.decision === "allow" ? "allow" : e.decision === "deny" ? "deny" : "pending"}>{e.decision ?? "pending"}</Badge></td>
              <td><code>{e.reason_code ?? "—"}</code></td><td>{fmtMs(e.latency_ms)}</td></tr>)}
          </tbody></table>)}
      </Card>
      <div className="grid2">
        <Card title="Stop events">
          {stops.length === 0 ? <p className="muted">The run was not stopped.</p> : stops.map((e) => <p key={e.seq}><Badge tone="stopped">stopped</Badge> {fmtTime(e.timestamp)} — {e.message}</p>)}
        </Card>
        <Card title="Measured policy latency">
          {!stats ? <p className="muted">No decided requests yet.</p> : (<>
            <p>n = {stats.count} · min {fmtMs(stats.min)} · p50 {fmtMs(stats.p50)} · p95 {fmtMs(stats.p95)} · max {fmtMs(stats.max)}</p>
            <p className="muted">Backend-measured policy evaluation time per request. It excludes network, gateway and tool time, so it is not an end-to-end figure.</p></>)}
        </Card>
      </div>
      <Card title="Sandbox and review evidence (from execution plane)">
        {impact && <p>Impact <code>{impact.impact_report_id}</code> · decision <b>{impact.decision}</b> · patch sha256 <code>{impact.patch_sha256}</code></p>}
        {exec.length === 0 ? <p className="muted">No execution-plane events recorded for this run.</p> : (
          <ul className="plain">{exec.map((e) => <li key={e.seq}><Badge tone="info">{e.event_type.replace("_", " ")}</Badge> {fmtTime(e.timestamp)} {e.message}{e.details && <details><summary>details</summary><Json value={e.details} /></details>}</li>)}</ul>)}
      </Card>
    </>
  );
}
