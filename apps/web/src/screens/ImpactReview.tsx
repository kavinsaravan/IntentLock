import { useCallback, useEffect, useState } from "react";
import { DiffView } from "../components/DiffView";
import { Badge, Card, Notice, Spinner, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { findImpactReportId, impactReportIds, toolEvents } from "../lib/events";
import { shortSha } from "../lib/format";
import type { ImpactReport, ImpactStatus } from "../lib/types";
import { useRunLive } from "../lib/useRunLive";

type Load<T> = { s: "idle" } | { s: "loading" } | { s: "unavailable"; why: string } | { s: "error"; why: string } | { s: "ok"; v: T };
const sevTone = (s: string) => (s === "critical" || s === "high" ? "deny" : s === "medium" ? "review" : "info") as "deny" | "review" | "info";
const BLOCKER_TEXT: Record<string, string> = {
  IMPACT_BLOCKED: "TwinGuard blocked this patch", ALREADY_REVIEWED: "a decision was already recorded", NOT_APPROVED: "no approval recorded",
  ALREADY_REQUESTED: "promotion was already requested", PATCH_HASH_MISMATCH: "patch bytes do not match the reported digest",
  PATCH_UNAVAILABLE: "patch artifact unavailable", STALE_BASE_COMMIT: "base commit no longer matches", CONTRACT_NOT_ACTIVE: "the contract was superseded or revoked",
  RUN_NOT_ELIGIBLE: "the run was stopped, blocked or failed", RUN_NOT_FOUND: "run not found", CONTRACT_NOT_FOUND: "contract not found",
  PROMOTER_NOT_CONFIGURED: "no patch promoter is configured on this backend", REVIEW_AUTH_NOT_CONFIGURED: "reviewer authentication is not configured",
};
const why = (codes: string[]) => codes.map((c) => BLOCKER_TEXT[c] ?? c).join("; ");

export function ImpactReview({ runId }: { runId: string }) {
  const { events } = useRunLive(runId);
  const detected = findImpactReportId(events);
  const reports = impactReportIds(events);
  const [selected, setSelected] = useState("");
  const [manual, setManual] = useState("");
  const id = selected || detected || manual.trim() || null;
  const [rep, setRep] = useState<Load<ImpactReport>>({ s: "idle" });
  const [patch, setPatch] = useState<Load<string>>({ s: "idle" });
  const [status, setStatus] = useState<ImpactStatus | null>(null);
  const [reviewer, setReviewer] = useState("demo-user");
  const [reviewToken, setReviewToken] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  const refreshStatus = useCallback(async (iid: string) => {
    try { setStatus(await api.impactStatus(iid)); } catch { setStatus(null); }
  }, []);

  useEffect(() => {
    if (!id) { setRep({ s: "idle" }); setPatch({ s: "idle" }); setStatus(null); return; }
    let off = false;
    setRep({ s: "loading" }); setPatch({ s: "loading" }); setActionErr(null);
    api.getImpact(id).then((v) => !off && setRep({ s: "ok", v }))
      .catch((e: unknown) => !off && setRep(e instanceof ApiError && e.unavailable
        ? { s: "unavailable", why: `GET /api/impact/${id} → ${e.message}` } : { s: "error", why: String(e) }));
    api.getPatch(id).then((v) => !off && setPatch({ s: "ok", v }))
      .catch((e: unknown) => !off && setPatch({ s: "unavailable", why: e instanceof ApiError ? e.message : String(e) }));
    refreshStatus(id);
    const t = setInterval(() => refreshStatus(id), 4000);
    return () => { off = true; clearInterval(t); };
  }, [id, refreshStatus]);

  async function act(kind: "approve" | "reject" | "promote") {
    if (rep.s !== "ok") return;
    const r = rep.v; setBusy(kind); setActionErr(null);
    try {
      if (kind === "promote") await api.promote(r.impact_report_id, { requested_by: reviewer, patch_sha256: r.patch_sha256 }, reviewToken);
      else await api.review(r.impact_report_id, kind, { reviewer, patch_sha256: r.patch_sha256, base_commit: r.base_commit }, reviewToken);
    } catch (e) {
      setActionErr(e instanceof ApiError ? (e.unavailable ? `${kind} endpoint unavailable (${e.message})` : `${kind} refused by backend: ${e.message}`) : String(e));
    } finally { setBusy(null); await refreshStatus(r.impact_report_id); }
  }

  const patchReq = toolEvents(events).filter((e) => e.tool === "propose_patch").at(-1);
  const r = rep.s === "ok" ? rep.v : null;
  const blocked = r?.decision === "block";
  const rv = status?.review ?? null;
  const pr = status?.promotion ?? null;

  return (
    <>
      <Stepper active={3} runId={runId} />
      {reports.length > 1 && <Card title="Patch attempts">
        <label className="field"><span>Impact report to inspect</span>
          <select value={id ?? ""} onChange={(e) => setSelected(e.target.value)}>
            {reports.map((reportId, index) => <option key={reportId} value={reportId}>
              {index === 0 ? "Latest" : `Earlier ${index}`} · {reportId}
            </option>)}
          </select>
        </label>
      </Card>}
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
              No <code>impact_report</code> event with an <code>impact_report_id</code> has been received for this run. TwinGuard belongs to the execution plane and submits reports to <code>POST /internal/impact</code>.
            </Notice>
          )}
          <label className="field"><span>Load an impact report by id</span>
            <input value={manual} onChange={(e) => setManual(e.target.value)} placeholder="impact_01" /></label>
        </Card>
      )}
      {id && rep.s === "loading" && <Spinner label={`Loading impact report ${id}…`} />}
      {id && rep.s === "unavailable" && <Notice tone="warn" title="Impact report unavailable">Not found or backend unreachable ({rep.why}).</Notice>}
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
          <ChecksCard r={r} />
          <Card title="Deterministic findings">
            {r.findings.length === 0 ? <p className="muted">No findings.</p> : (
              <ul className="findings">{r.findings.map((f, i) => <li key={i}><Badge tone={sevTone(f.severity)}>{f.severity}</Badge> <code>{f.code}</code><div>{f.message}</div>{f.evidence && <small className="muted">{f.evidence}</small>}</li>)}</ul>)}
          </Card>
        </div>
        <Card title="Exact patch under review">
          {patch.s === "loading" && <Spinner label="Loading patch…" />}
          {patch.s === "unavailable" && <Notice tone="warn" title="Patch text unavailable">The backend could not serve a patch whose SHA-256 matches this report ({patch.why}). Do not approve what you cannot read.</Notice>}
          {patch.s === "ok" && <DiffView patch={patch.v} />}
        </Card>
        <Card title="Human review">
          {blocked && <Notice tone="error" title="Blocked by TwinGuard">Blocked patches cannot be approved or promoted (enforced by the backend).</Notice>}
          <label className="field"><span>Reviewer</span><input value={reviewer} onChange={(e) => setReviewer(e.target.value)} maxLength={128} /></label>
          <label className="field"><span>Reviewer token</span><input type="password" autoComplete="off" value={reviewToken} onChange={(e) => setReviewToken(e.target.value)} /></label>
          {status && <p className="muted">Reviewer policy: {status.reviewer_policy}.</p>}
          <div className="row">
            <button className="btn primary" disabled={!status?.can_approve || patch.s !== "ok" || !reviewer.trim() || !reviewToken || busy !== null} onClick={() => act("approve")}>Approve exact patch {shortSha(r.patch_sha256, 8)}</button>
            <button className="btn" disabled={!!rv || !reviewer.trim() || !reviewToken || busy !== null} onClick={() => act("reject")}>Reject</button>
            <button className="btn" disabled={!status?.can_promote || !reviewer.trim() || !reviewToken || busy !== null} onClick={() => act("promote")}>Request promotion</button>
          </div>
          {status && !status.can_approve && !rv && <p className="muted">Approval unavailable: {why(status.approve_blockers)}.</p>}
          {status && rv?.decision === "approve" && !status.can_promote && !pr && <p className="muted">Promotion unavailable: {why(status.promote_blockers)}.</p>}
          <p className="muted">Approval records your identity, the contract version, base commit <code>{shortSha(r.base_commit, 8)}</code> and this SHA-256. A separate request asks the patch promoter to apply exactly that artifact; the promoter re-verifies both before changing anything.</p>
          {busy && <Spinner label={`Sending ${busy}…`} />}
          {actionErr && <Notice tone="error" title="Action not completed">{actionErr}</Notice>}
          {rv && <Notice tone={rv.decision === "approve" ? "info" : "warn"} title={`Review recorded: ${rv.decision.toUpperCase()}`}>
            <code>{rv.review_id}</code> by <b>{rv.reviewer}</b> at {rv.reviewed_at} · contract v{rv.contract_version} · base <code>{shortSha(rv.base_commit, 8)}</code> · digest <code>{shortSha(rv.patch_sha256, 12)}…</code></Notice>}
          {pr && <Notice tone={pr.status === "promoted" ? "info" : pr.status === "requested" ? "warn" : "error"} title={`Promotion ${pr.status.toUpperCase()}`}>
            requested by {pr.requested_by}{pr.completed_at ? `, completed ${pr.completed_at}` : ""}{pr.reason_code ? ` · ${pr.reason_code}` : ""}{pr.message ? ` · ${pr.message}` : ""}{pr.applied_patch_sha256 ? ` · applied digest ${shortSha(pr.applied_patch_sha256, 12)}…` : ""}</Notice>}
        </Card>
      </>)}
    </>
  );
}

/** Checks TwinGuard selected from the changed paths, why, and what no test covers. */
export function ChecksCard({ r }: { r: Pick<ImpactReport, "tests" | "coverage"> }) {
  const uncovered = r.coverage?.uncovered_paths ?? [];
  return (
    <Card title="Selected checks">
      {r.tests.length === 0 ? <p className="muted">No checks were run.</p> : (
        <table className="tbl"><thead><tr><th>Check</th><th>Why selected</th><th>Result</th><th>Exit</th><th>Time</th></tr></thead><tbody>
          {r.tests.map((t) => <tr key={t.test_id}><td><code>{t.test_id}</code></td><td>{t.reason ? <small className="muted">{t.reason}</small> : "—"}</td><td><Badge tone={t.status === "passed" ? "ok" : "deny"}>{t.status}</Badge></td><td>{t.exit_code ?? "—"}</td><td>{t.duration_ms} ms</td></tr>)}
        </tbody></table>)}
      {uncovered.length > 0 && (
        <Notice tone="error" title="Missing test coverage">
          No approved test covers: {uncovered.map((p) => <code key={p}> {p}</code>)}. Passing checks do not prove these paths are safe.
        </Notice>)}
    </Card>
  );
}
