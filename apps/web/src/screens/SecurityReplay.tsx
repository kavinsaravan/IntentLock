import { useEffect, useState } from "react";
import { DiffView } from "../components/DiffView";
import { Badge, Card, Notice, Spinner } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { fmtMs, shortSha } from "../lib/format";
import {
  type Attempt, type ReplayCatalog, type ReplayView, type TimelineItem,
  blockerText, EVIDENCE_LABEL, promotionLabel, replayHref, reviewLabel, STAGE_LABEL, statusLabel,
} from "../lib/replay";

const fmtWhen = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString([], { hour12: false });
};

function S({ status }: { status: string }) {
  const [label, tone] = statusLabel(status);
  return <Badge tone={tone}>{label}</Badge>;
}

const sevTone = (s: string) => (s === "critical" || s === "high" ? "deny" : s === "medium" ? "review" : "info") as "deny" | "review" | "info";

// ------------------------------------------------------------------------------------------- run selector
function RunSelector({ catalog, selected, onOpen }: { catalog: ReplayCatalog; selected: string | null; onOpen: (id: string) => void }) {
  const [manual, setManual] = useState("");
  const valid = /^[A-Za-z0-9_-]{1,100}$/.test(manual.trim());
  const body = (<>
      <div className="rp-sources">
        {catalog.sources.length === 0 && <Badge tone="na">No evidence store configured</Badge>}
        {catalog.sources.map((s) => (
          <span key={s.id + s.label}><Badge tone={s.available ? "ok" : "na"}>{s.available ? "available" : "unavailable"}</Badge> {s.label}
            {s.available ? ` · ${s.runs ?? 0} recorded` : s.reason ? ` · ${s.reason}` : ""}</span>
        ))}
        <span><Badge tone={catalog.live_lookup.persistent ? "ok" : "review"}>live lookup</Badge> {catalog.live_lookup.store}</span>
      </div>
      {catalog.runs.length === 0
        ? <p className="muted">No recorded runs were found in the configured evidence stores. Runs appear here after Impact Preview writes impact reports or the audit export runs.</p>
        : (
          <table className="tbl rp-runs"><thead><tr><th>Run</th><th>Outcome</th><th>Evidence</th><th>Status</th><th>Mission</th><th>Last activity</th></tr></thead>
            <tbody>
              {catalog.runs.map((r) => {
                const [ev, evTone] = EVIDENCE_LABEL[r.evidence];
                return (
                  <tr key={r.source + r.run_id} className={r.run_id === selected ? "on" : ""}>
                    <td><a href={replayHref(r.run_id, r.source)}><code>{r.run_id}</code></a></td>
                    <td>{r.outcome_title}</td>
                    <td><Badge tone={evTone}>{ev}</Badge> <span className="muted">{r.source}</span></td>
                    <td>{r.status ? <S status={r.status} /> : <Badge tone="na">Not recorded</Badge>}</td>
                    <td>{r.mission ?? <span className="muted">not recorded</span>}</td>
                    <td className="muted">{fmtWhen(r.last_activity)}</td>
                  </tr>
                );
              })}
            </tbody></table>)}
      {catalog.truncated && <p className="muted">Showing the first 100 runs.</p>}
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (valid) onOpen(manual.trim()); }}>
        <input aria-label="Run ID" placeholder="Open a run by ID (also finds live runs not yet exported)" value={manual}
          onChange={(e) => setManual(e.target.value)} maxLength={100} style={{ flex: 1, width: "auto" }} />
        <button className="btn" type="submit" disabled={!valid}>Open</button>
      </form>
  </>);
  const count = `${catalog.runs.length} recorded run${catalog.runs.length === 1 ? "" : "s"}`;
  if (selected) {
    return (
      <details className="card rp-picker">
        <summary><b>Change run</b> <span className="muted">· replaying <code>{selected}</code> · {count}</span></summary>
        <div className="rp-picker-body">{body}</div>
      </details>
    );
  }
  return <Card title="Recorded runs" right={<span className="muted">{count}</span>}>{body}</Card>;
}

// ----------------------------------------------------------------------------------------------- outcome
function Outcome({ v }: { v: ReplayView }) {
  const [ev, evTone] = EVIDENCE_LABEL[v.evidence.kind];
  return (
    <section className={`rp-outcome rp-${v.outcome.tone}`} aria-label="Final outcome">
      <div className="rp-outcome-k">Final outcome</div>
      <h1>{v.outcome.title}</h1>
      <p>{v.outcome.detail}</p>
      <div className="rp-meta">
        <span>Run <code>{v.run_id}</code></span>
        {v.run && <span>Status <S status={v.run.status} /></span>}
        {v.run && <span>Contract <code>{v.run.contract_id}</code> v{v.run.contract_version}</span>}
        <span><Badge tone={evTone}>{ev}</Badge> {v.evidence.source_label}</span>
      </div>
      <div className="rp-meta">
        {v.run && <span>Started {fmtWhen(v.run.started_at ?? v.run.created_at)}</span>}
        {v.run && <span>Ended {v.run.completed_at || v.run.stopped_at ? fmtWhen(v.run.completed_at ?? v.run.stopped_at) : "not recorded"}</span>}
        <span>Final audit {v.evidence.audit
          ? <><code>{v.evidence.audit.key}</code> sha256 <code title={v.evidence.audit.sha256}>{shortSha(v.evidence.audit.sha256, 16)}…</code></>
          : <Badge tone="na">Evidence unavailable</Badge>}</span>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------------------------------- two checks
function TwoChecks({ v, a }: { v: ReplayView; a: Attempt | null }) {
  const auth = v.authorization;
  const authStatus = a ? a.authorization.status : !auth.available ? "unavailable" : auth.denied ? "denied" : auth.allowed ? "allowed" : "not_recorded";
  const split = a && a.authorization.status === "allowed" && a.consequence.status === "blocked";
  return (
    <Card title="Authorization versus consequence">
      {split && <Notice tone="warn" title="Allowed is not the same as safe">IntentLock allowed the <code>propose_patch</code> action under the mission contract; Impact Preview then rejected what the patch would do.</Notice>}
      <div className="rp-checks">
        <div className="rp-check">
          <div className="rp-check-h"><span className="rp-num">1</span><div><b>Action authorization</b><small>Was the agent allowed to perform the requested operation under its mission contract?</small></div></div>
          <div className="rp-verdict"><S status={authStatus} /></div>
          {a ? <p className="muted">{a.authorization.basis}{a.authorization.reason_code ? ` Reason: ${a.authorization.reason_code}.` : ""}{a.authorization.latency_ms != null ? ` Policy time ${fmtMs(a.authorization.latency_ms)}.` : ""}</p>
            : <p className="muted">{auth.available ? "Recorded policy decisions for this run's tool requests." : "Control-plane authorization events are not part of this evidence."}</p>}
          {auth.available && <p>Run total: <b>{auth.allowed}</b> allowed · <b className={auth.denied ? "bad" : ""}>{auth.denied}</b> denied{auth.pending ? <> · <b>{auth.pending}</b> undecided</> : null}</p>}
          {auth.denied_requests.length > 0 && <ul className="plain">{auth.denied_requests.map((d) => (
            <li key={d.seq}><Badge tone="deny">Denied</Badge> <code>{d.tool ?? "?"}</code> {d.arguments && <code>{JSON.stringify(d.arguments)}</code>} <code className="bad">{d.reason_code}</code> <span className="muted">event #{d.seq}</span></li>))}</ul>}
        </div>
        <div className="rp-check">
          <div className="rp-check-h"><span className="rp-num">2</span><div><b>Consequence verification</b><small>Did the resulting proposed change satisfy trusted tests and security requirements?</small></div></div>
          {a ? (<>
            <div className="rp-verdict"><S status={a.consequence.status} /></div>
            <p className="muted">{a.consequence.basis}</p>
            {a.consequence.tests_recorded === 0 ? <p><Badge tone="na">No trusted test result recorded</Badge></p>
              : <p>Trusted tests: {a.consequence.failing_tests.length === 0 ? <b>all {a.consequence.tests_recorded} passed</b> : <b className="bad">{a.consequence.failing_tests.join(", ")} failed</b>}</p>}
            {a.consequence.blocking_findings.length > 0 && <p>Blocking findings: {a.consequence.blocking_findings.map((c) => <code key={c} className="bad">{c} </code>)}</p>}
          </>) : <><div className="rp-verdict"><Badge tone="na">No patch evaluated</Badge></div><p className="muted">No Impact Preview report is recorded for this run.</p></>}
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------- timeline
function Timeline({ items, events }: { items: TimelineItem[]; events: boolean }) {
  return (
    <Card title="Security timeline" right={<span className="muted">{items.length} recorded entr{items.length === 1 ? "y" : "ies"}</span>}>
      {!events && <Notice tone="info" title="Partial timeline">Control-plane events were not exported for this run. Only Impact Preview artifacts are shown; no other stage is inferred.</Notice>}
      <ol className="rp-timeline">
        {items.map((t, i) => (
          <li key={`${t.seq ?? "x"}-${i}`} className={`rp-ti rp-ti-${statusLabel(t.status)[1]}`}>
            <div className="rp-ti-main">
              <span className="rp-stage">{STAGE_LABEL[t.stage] ?? t.stage}</span>
              <b>{t.title}</b>
              <S status={t.status} />
              {t.tool && t.arguments && <code>{JSON.stringify(t.arguments)}</code>}
              {t.reason_code && <code className={t.status === "denied" ? "bad" : ""}>{t.reason_code}</code>}
              <span className="rp-when">{fmtWhen(t.timestamp)} · {t.source}</span>
            </div>
            {t.detail && <div className="rp-ti-detail">{t.detail}</div>}
            {t.tests && t.tests.length > 0 && <div className="rp-ti-detail">{t.tests.map((x) => <span key={x.test_id}>trusted test <code>{x.test_id}</code> <S status={x.status === "passed" ? "passed" : "failed"} /> exit {x.exit_code ?? "—"} </span>)}</div>}
            {t.findings && t.findings.length > 0 && <div className="rp-ti-detail">{t.findings.map((f, j) => <span key={j}><Badge tone={sevTone(f.severity)}>{f.severity}</Badge> <code>{f.code}</code> </span>)}</div>}
          </li>
        ))}
      </ol>
    </Card>
  );
}

// --------------------------------------------------------------------------------------------- patch etc.
const PATCH_STATUS: Record<string, [string, "ok" | "deny" | "na"]> = {
  verified: ["SHA-256 verified against artifact", "ok"], hash_mismatch: ["Hash mismatch — not displayed", "deny"],
  unavailable: ["Evidence unavailable", "na"], key_invalid: ["Artifact reference rejected", "deny"],
};

function PatchCard({ a }: { a: Attempt }) {
  const p = a.patch;
  const [label, tone] = PATCH_STATUS[p.status];
  return (
    <Card title="Patch inspection" right={<Badge tone={tone}>{label}</Badge>}>
      <dl className="kv">
        <dt>Affected files</dt><dd>{p.files.length > 0 ? p.files.map((f) => <div key={f.path}><code>{f.path}</code> <span className="muted">{f.status}</span> <span className="add">+{f.added}</span> <span className="del">−{f.deleted}</span>{f.withheld && <> <Badge tone="na">content withheld</Badge></>}</div>)
          : <span className="muted">{a.summary.files_modified + a.summary.files_created + a.summary.files_deleted + a.summary.files_renamed} file(s) per impact summary; diff not available</span>}</dd>
        <dt>Patch SHA-256</dt><dd><code>{p.sha256}</code></dd>
        <dt>Expected base</dt><dd><code>{p.base_commit}</code></dd>
        <dt>Artifact</dt><dd><code>{p.key}</code>{p.bytes != null && <span className="muted"> · {p.bytes} bytes</span>}</dd>
      </dl>
      {p.withheld_files.length > 0 && <Notice tone="info" title="Protected content withheld">Diff hunks for {p.withheld_files.map((f) => <code key={f}>{f} </code>)} are not displayed because the path is protected.</Notice>}
      {p.redactions > 0 && <Notice tone="info">{p.redactions} credential-like value{p.redactions === 1 ? " was" : "s were"} redacted from the displayed diff.</Notice>}
      {p.truncated && <Notice tone="warn">The patch is larger than the display limit; only the beginning is shown. The SHA-256 check covered the full artifact.</Notice>}
      {p.status !== "verified" && <Notice tone={p.status === "unavailable" ? "info" : "error"} title={label}>{p.reason ?? "The stored patch cannot be shown."}</Notice>}
      {p.status === "verified" && p.text !== null && (p.parsed ? <DiffView patch={p.text} /> : p.text ? <pre className="json">{p.text}</pre> : null)}
    </Card>
  );
}

function TestsCard({ a }: { a: Attempt }) {
  return (
    <Card title="Trusted tests">
      {a.tests.length === 0 ? <Notice tone="warn" title="No trusted test result recorded">This is missing evidence, not a passing result.</Notice> : (
        <table className="tbl"><thead><tr><th>Test</th><th>Result</th><th>Exit</th><th>Time</th></tr></thead><tbody>
          {a.tests.map((t) => <tr key={t.test_id}><td><code>{t.test_id}</code></td><td><S status={t.status === "passed" ? "passed" : "failed"} /> <span className="muted">{t.status}</span></td><td>{t.exit_code ?? "—"}</td><td>{t.duration_ms} ms</td></tr>)}
        </tbody></table>)}
      {a.tests.map((t) => (
        <details key={t.test_id} className="rp-out">
          <summary>Output of <code>{t.test_id}</code> · {t.output_status === "available" ? <code>{t.output_key}</code> : <Badge tone="na">{t.output_status === "not_recorded" ? "Not recorded" : t.output_status === "key_invalid" ? "Reference rejected" : "Evidence unavailable"}</Badge>}</summary>
          {t.output && <pre className="json">{t.output_truncated ? "…\n" : ""}{t.output}</pre>}
        </details>
      ))}
    </Card>
  );
}

function FindingsCard({ a }: { a: Attempt }) {
  return (
    <Card title="Impact findings" right={<>Impact Preview decision <S status={a.consequence.status} /></>}>
      {a.findings.length === 0 ? <p className="muted">Impact Preview recorded no findings for this patch.</p> : (
        <ul className="findings">{a.findings.map((f, i) => <li key={i}><Badge tone={sevTone(f.severity)}>{f.severity}</Badge> <code>{f.code}</code><div>{f.message}</div>{f.evidence && <small className="muted">evidence: {f.evidence}</small>}</li>)}</ul>)}
      <p className="muted">Report <code>{a.impact_report_id}</code> · sandbox <code>{a.sandbox_id}</code> · {fmtWhen(a.created_at)} · +{a.summary.lines_added} / −{a.summary.lines_deleted} lines</p>
    </Card>
  );
}

function ReviewCard({ v, a }: { v: ReplayView; a: Attempt }) {
  const [rl, rt] = reviewLabel(a);
  const [pl, pt] = promotionLabel(a);
  const e = a.eligibility;
  return (
    <Card title="Human review and promotion">
      <div className="rp-checks">
        <div>
          <h3>Review eligibility</h3>
          {a.consequence.status === "blocked" ? <p><Badge tone="deny">Not promotable</Badge> {blockerText(e.promote_blockers.length ? e.promote_blockers : ["IMPACT_BLOCKED"])}.</p>
            : !e.recorded ? <p><Badge tone="na">Evidence unavailable</Badge> <span className="muted">{e.basis}</span></p>
            : a.review.status !== "none" ? <p><Badge tone="info">Reviewed</Badge></p>
            : <p>{e.can_approve ? <Badge tone="review">Eligible for review</Badge> : <Badge tone="stopped">Not approvable</Badge>} {e.approve_blockers.length > 0 && <span className="muted">Recorded blockers: {blockerText(e.approve_blockers)}.</span>}</p>}
          <h3>Review</h3>
          <p><Badge tone={rt}>{rl}</Badge> {a.review.reviewer && <>by <b>{a.review.reviewer}</b> at {fmtWhen(a.review.reviewed_at)}</>}</p>
          {a.review.patch_sha256 && <p className="muted">Bound to SHA-256 <code>{shortSha(a.review.patch_sha256, 16)}…</code> on base <code>{shortSha(a.review.base_commit ?? "", 12)}</code> · {a.review.matches_patch ? "matches this patch" : <b className="bad">does not match this patch</b>}</p>}
          {!a.review.reviewer && <p className="muted">{a.review.basis}</p>}
        </div>
        <div>
          <h3>Promotion</h3>
          <p><Badge tone={pt}>{pl}</Badge> {a.promotion.requested_by && <>requested by <b>{a.promotion.requested_by}</b></>}{a.promotion.reason_code && <> · <code className="bad">{a.promotion.reason_code}</code></>}</p>
          {a.promotion.applied_patch_sha256 && <p className="muted">Applied SHA-256 <code>{shortSha(a.promotion.applied_patch_sha256, 16)}…</code> · {a.promotion.applied_matches_approved ? "identical to the approved patch" : <b className="bad">differs from the approved patch</b>}</p>}
          {a.promotion.status !== "promoted" && <p className="muted">{a.promotion.basis}</p>}
          <h3>Final audit reference</h3>
          {v.evidence.audit ? <p><code>{v.evidence.audit.key}</code><br /><span className="muted">sha256 <code>{shortSha(v.evidence.audit.sha256, 16)}…</code> · {v.evidence.audit.bytes} bytes · {v.evidence.audit.source}</span></p>
            : <p><Badge tone="na">Evidence unavailable</Badge> <span className="muted">No final audit export for this run.</span></p>}
        </div>
      </div>
    </Card>
  );
}

// -------------------------------------------------------------------------------------------- full view
export function ReplayDetail({ v }: { v: ReplayView }) {
  const focus = v.attempts.findIndex((x) => x.impact_report_id === v.outcome.impact_report_id);
  const [idx, setIdx] = useState(focus >= 0 ? focus : Math.max(0, v.attempts.length - 1));
  const a = v.attempts[idx] ?? null;
  return (
    <>
      <Outcome v={v} />
      {v.attempts.length > 1 && (
        <div className="rp-attempts" role="tablist" aria-label="Patch attempts">
          {v.attempts.map((x, i) => (
            <button key={x.impact_report_id} role="tab" aria-selected={i === idx} className={`btn ${i === idx ? "primary" : ""}`} onClick={() => setIdx(i)}>
              Attempt {i + 1} · {statusLabel(x.consequence.status)[0]}
            </button>
          ))}
        </div>
      )}
      <TwoChecks v={v} a={a} />
      <Timeline items={v.timeline} events={v.evidence.events_available} />
      {a && (<>
        <PatchCard a={a} />
        <div className="grid2"><TestsCard a={a} /><FindingsCard a={a} /></div>
        <ReviewCard v={v} a={a} />
      </>)}
      {(v.claims.length > 0 || v.contract) && (
        <div className="grid2">
          <Card title="Recorded integrity evidence">
            {v.claims.length === 0 ? <p className="muted">No integrity statement is supported by the stored records.</p>
              : <ul className="plain">{v.claims.map((c, i) => <li key={i}>{c.claim}<br /><small className="muted">{c.basis}</small></li>)}</ul>}
          </Card>
          <Card title="Mission contract">
            {!v.contract ? <p className="muted">Not part of this evidence.</p> : (<div className="rp-tags">
              <p>{v.contract.mission}</p>
              <p className="muted"><code>{v.contract.contract_id}</code> v{v.contract.version} · approved by {v.contract.created_by} · {fmtWhen(v.contract.approved_at)}</p>
              <p>Tools: {v.contract.allowed_tools.map((t) => <code key={t}>{t} </code>)}</p>
              <p>Allowed: {v.contract.allowed_paths.map((t) => <code key={t}>{t} </code>)}</p>
              <p>Protected: {v.contract.protected_paths.map((t) => <code key={t}>{t} </code>)}</p>
            </div>)}
          </Card>
        </div>
      )}
      {v.limitations.length > 0 && (
        <Card title="Evidence limitations"><ul className="plain">{v.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul></Card>
      )}
    </>
  );
}

type Load<T> = { s: "idle" } | { s: "loading" } | { s: "missing" } | { s: "error"; why: string } | { s: "ok"; v: T };

export function SecurityReplay({ runId, source }: { runId: string | null; source: string }) {
  const [catalog, setCatalog] = useState<Load<ReplayCatalog>>({ s: "loading" });
  const [view, setView] = useState<Load<ReplayView>>({ s: "idle" });

  useEffect(() => {
    let off = false;
    api.replayRuns().then((v) => !off && setCatalog({ s: "ok", v }))
      .catch((e: unknown) => !off && setCatalog({ s: "error", why: e instanceof ApiError ? e.message : String(e) }));
    return () => { off = true; };
  }, []);

  useEffect(() => {
    if (!runId) { setView({ s: "idle" }); return; }
    let off = false;
    setView({ s: "loading" });
    api.replayRun(runId, source).then((v) => !off && setView({ s: "ok", v }))
      .catch((e: unknown) => !off && setView(e instanceof ApiError && e.status === 404 ? { s: "missing" }
        : { s: "error", why: e instanceof ApiError ? e.message : String(e) }));
    return () => { off = true; };
  }, [runId, source]);

  return (
    <>
      <div className="rp-head">
        <h1>Security Replay</h1>
        <p className="muted">Read-only reconstruction of recorded security decisions. Nothing on this page is re-evaluated or inferred.</p>
      </div>
      {catalog.s === "loading" && <Spinner label="Loading recorded runs…" />}
      {catalog.s === "error" && <Notice tone="error" title="Recorded runs unavailable">{catalog.why}</Notice>}
      {catalog.s === "ok" && <RunSelector catalog={catalog.v} selected={runId} onOpen={(id) => { location.hash = replayHref(id); }} />}
      {view.s === "loading" && <Spinner label={`Loading evidence for ${runId}…`} />}
      {view.s === "missing" && <Notice tone="warn" title="Evidence unavailable">No recorded evidence for <code>{runId}</code> in the configured evidence stores or the live control-plane store.</Notice>}
      {view.s === "error" && <Notice tone="error" title="Could not load replay">{view.why}</Notice>}
      {view.s === "ok" && <ReplayDetail key={view.v.run_id + view.v.evidence.source} v={view.v} />}
      {view.s === "idle" && catalog.s === "ok" && catalog.v.runs.length > 0 && <p className="muted">Select a run to replay its security decisions.</p>}
    </>
  );
}
