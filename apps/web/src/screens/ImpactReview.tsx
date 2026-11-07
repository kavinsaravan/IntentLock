import { useEffect, useState } from "react";
import { DiffView } from "../components/DiffView";
import { Badge, Card, Json, Notice, Spinner, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { findImpactReportId, toolEvents } from "../lib/events";
import { shortSha } from "../lib/format";
import type { ImpactReport } from "../lib/types";
import { useRunLive } from "../lib/useRunLive";

type Load<T> = { s: "idle" } | { s: "loading" } | { s: "unavailable"; why: string } | { s: "error"; why: string } | { s: "ok"; v: T };
const sevTone = (s: string) => (s === "critical" || s === "high" ? "deny" : s === "medium" ? "review" : "info") as "deny" | "review" | "info";

export function ImpactReview({ runId }: { runId: string }) {
  const { events } = useRunLive(runId);
  const detected = findImpactReportId(events);
  const [manual, setManual] = useState("");
  const id = detected ?? (manual.trim() || null);
  const [rep, setRep] = useState<Load<ImpactReport>>({ s: "idle" });
  const [patch, setPatch] = useState<Load<string>>({ s: "idle" });
  const [reviewer, setReviewer] = useState("demo-user");
  const [action, setAction] = useState<{ s: "idle" | "busy" | "done" | "err"; msg?: string; body?: Record<string, unknown>; kind?: string }>({ s: "idle" });

  useEffect(() => {
    if (!id) { setRep({ s: "idle" }); setPatch({ s: "idle" }); return; }
    let off = false;
    setRep({ s: "loading" }); setPatch({ s: "loading" });
    api.getImpact(id).then((v) => !off && setRep({ s: "ok", v }))
      .catch((e: unknown) => !off && setRep(e instanceof ApiError && e.unavailable
        ? { s: "unavailable", why: `GET /api/impact/${id} → ${e.message}` } : { s: "error", why: String(e) }));
    api.getPatch(id).then((v) => !off && setPatch({ s: "ok", v }))
      .catch((e: unknown) => !off && setPatch({ s: "unavailable", why: e instanceof ApiError ? e.message : String(e) }));
    return () => { off = true; };
  }, [id]);

  async function review(kind: "approve" | "reject") {
    if (rep.s !== "ok") return;
    setAction({ s: "busy", kind });
    try {
      const body = await api.review(rep.v.impact_report_id, kind, { reviewer, patch_sha256: rep.v.patch_sha256, base_commit: rep.v.base_commit });
      setAction({ s: "done", kind, body, msg: `Backend accepted ${kind}.` });
    } catch (e) { setAction({ s: "err", kind, msg: e instanceof ApiError ? (e.unavailable ? `${kind} endpoint is not available on this backend yet (${e.message}).` : e.message) : String(e) }); }
  }
  async function promote() {
    if (rep.s !== "ok") return;
    setAction({ s: "busy", kind: "promote" });
    try {
      const body = await api.promote(rep.v.impact_report_id, { patch_sha256: rep.v.patch_sha256 });
      setAction({ s: "done", kind: "promote", body, msg: "Backend returned a promotion result." });
    } catch (e) { setAction({ s: "err", kind: "promote", msg: e instanceof ApiError ? e.message : String(e) }); }
  }

  const patchReq = toolEvents(events).filter((e) => e.tool === "propose_patch").at(-1);
  const r = rep.s === "ok" ? rep.v : null;
  const blocked = r?.decision === "block";
  const busy = action.s === "busy";
  const approved = action.s === "done" && action.kind === "approve";

  return (
    <>
      <Stepper active={3} runId={runId} />
      <Card title="Decisions">
        <div className="grid2">
          <div><h3>IntentLock (authorization)</h3>
            {patchReq
              ? <p><Badge tone={patchReq.decision === "allow" ? "allow" : patchReq.decision === "deny" ? "deny" : "pending"}>{(patchReq.decision ?? "evaluating").toUpperCase()}</Badge> latest <code>propose_patch</code> request{patchReq.reason_code && <> · <code>{patchReq.reason_code}</code></>}</p>
              : <p className="muted">No <code>propose_patch</code> request recorded for this run.</p>}</div>
          <div><h3>TwinGuard (consequence inspection)</h3>
            {r ? <p><Badge tone={blocked ? "deny" : r.decision === "pass" ? "ok" : "review"}>{r.decision.replace("_", " ").toUpperCase()}</Badge>{r.decision === "pass" && <span className="muted"> may proceed to human review — never auto-applied</span>}</p>
              : <p className="muted">No impact report loaded.</p>}</div>
        </div>
      </Card>

      {!detected && (
        <Card title="TwinGuard impact report">
          {!id && (
            <Notice tone="info" title="No impact report yet">
              No <code>impact_report</code> event with an <code>impact_report_id</code> has been received for this run. TwinGuard belongs to the execution plane and reports through <code>POST /internal/events</code>.
            </Notice>
          )}
          <label className="field"><span>Load an impact report by id</span>
            <input value={manual} onChange={(e) => setManual(e.target.value)} placeholder="impact_01" /></label>
        </Card>
      )}
      {id && rep.s === "loading" && <Spinner label={`Loading impact report ${id}…`} />}
      {id && rep.s === "unavailable" && <Notice tone="warn" title="Impact report unavailable">The impact endpoint is not reachable or not implemented yet ({rep.why}). This screen fills in automatically once the execution plane serves <code>GET /api/impact/{"{id}"}</code>.</Notice>}
      {id && rep.s === "error" && <Notice tone="error" title="Could not load impact report">{rep.why}</Notice>}

      {r && (<>
        <Card title="Evidence">
          <dl className="kv">
            <dt>Report</dt><dd><code>{r.impact_report_id}</code></dd>
            <dt>Base commit</dt><dd><code>{shortSha(r.base_commit, 12)}</code></dd>
            <dt>Patch SHA-256</dt><dd><code title={r.patch_sha256}>{shortSha(r.patch_sha256, 16)}…</code></dd>
            <dt>Sandbox</dt><dd><code>{r.sandbox_id}</code></dd>
          </dl>
          <div className="tiles">
            {([["created", r.summary.files_created], ["modified", r.summary.files_modified], ["deleted", r.summary.files_deleted], ["renamed", r.summary.files_renamed]] as const).map(([k, v]) => <div key={k} className="tile"><b>{v}</b><span>files {k}</span></div>)}
            <div className="tile"><b className="add">+{r.summary.lines_added}</b><span>lines added</span></div>
            <div className="tile"><b className="del">−{r.summary.lines_deleted}</b><span>lines deleted</span></div>
          </div>
        </Card>
        <div className="grid2">
          <Card title="Approved tests">
            {r.tests.length === 0 ? <p className="muted">No tests recorded.</p> : (
              <table className="tbl"><thead><tr><th>Test</th><th>Result</th><th>Exit</th><th>Time</th></tr></thead><tbody>
                {r.tests.map((t) => <tr key={t.test_id}><td><code>{t.test_id}</code></td><td><Badge tone={t.status === "passed" ? "ok" : "deny"}>{t.status}</Badge></td><td>{t.exit_code ?? "—"}</td><td>{t.duration_ms} ms</td></tr>)}
              </tbody></table>)}
          </Card>
          <Card title="Deterministic findings">
            {r.findings.length === 0 ? <p className="muted">No findings.</p> : (
              <ul className="findings">{r.findings.map((f, i) => <li key={i}><Badge tone={sevTone(f.severity)}>{f.severity}</Badge> <code>{f.code}</code><div>{f.message}</div>{f.evidence && <small className="muted">{f.evidence}</small>}</li>)}</ul>)}
          </Card>
        </div>
        <Card title="Exact patch under review">
          {patch.s === "loading" && <Spinner label="Loading patch…" />}
          {patch.s === "unavailable" && <Notice tone="warn" title="Patch text unavailable">Expected <code>GET /api/impact/{r.impact_report_id}/patch</code> to return the reviewed patch (text). Without it the diff cannot be shown; do not approve what you cannot read.</Notice>}
          {patch.s === "ok" && <DiffView patch={patch.v} />}
        </Card>
        <Card title="Human review">
          {blocked && <Notice tone="error" title="Blocked by TwinGuard">Blocked patches cannot be approved or promoted.</Notice>}
          <label className="field"><span>Reviewer</span><input value={reviewer} onChange={(e) => setReviewer(e.target.value)} maxLength={128} /></label>
          <div className="row">
            <button className="btn primary" disabled={blocked || patch.s !== "ok" || !reviewer.trim() || busy} onClick={() => review("approve")}>Approve exact patch {shortSha(r.patch_sha256, 8)}</button>
            <button className="btn" disabled={!reviewer.trim() || busy} onClick={() => review("reject")}>Reject</button>
            <button className="btn" disabled={blocked || !approved || busy} onClick={promote}>Promote approved patch</button>
          </div>
          <p className="muted">Approval binds your decision to base commit <code>{shortSha(r.base_commit, 8)}</code> and this SHA-256. Promotion applies only that artifact after the backend verifies both.</p>
          {action.s === "busy" && <Spinner label={`Sending ${action.kind}…`} />}
          {action.s === "done" && <Notice tone="info" title={action.msg}><Json value={action.body} /></Notice>}
          {action.s === "err" && <Notice tone="error" title={`${action.kind} failed`}>{action.msg}</Notice>}
        </Card>
      </>)}
    </>
  );
}
