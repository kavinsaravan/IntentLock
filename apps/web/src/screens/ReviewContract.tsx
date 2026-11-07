import { useState } from "react";
import { Badge, Card, Notice, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { lines } from "../lib/format";
import type { ApprovedTest, ContractDraft, Contract } from "../lib/types";
import type { DraftState } from "./CreateMission";

const TOOLS = ["list_files", "read_file", "search_code", "propose_patch", "run_approved_test"];
type TestRow = { id: string; argv: string; timeout: number };
const num = (v: string) => (v === "" ? 0 : Number(v));

export function ReviewContract({ state }: { state: DraftState | null }) {
  if (!state) return (<><Stepper active={1} /><Notice tone="info" title="No draft to review">Start from <a href="#/">Create mission</a>.</Notice></>);
  return <Form state={state} />;
}

function Form({ state }: { state: DraftState }) {
  const d0 = state.draft;
  const [mission, setMission] = useState(d0.mission);
  const [tools, setTools] = useState<string[]>(d0.allowed_tools);
  const [allowed, setAllowed] = useState(d0.allowed_paths.join("\n"));
  const [protectedP, setProtectedP] = useState(d0.protected_paths.join("\n"));
  const [tests, setTests] = useState<TestRow[]>(Object.entries(d0.approved_tests).map(([id, t]) => ({ id, argv: t.argv.join(" "), timeout: t.timeout_seconds })));
  const [limits, setLimits] = useState(d0.limits);
  const [exportsTxt, setExportsTxt] = useState(d0.required_exports.join("\n"));
  const [approver, setApprover] = useState("demo-user");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<Contract | null>(null);

  function build(): ContractDraft {
    const approved: Record<string, ApprovedTest> = {};
    for (const t of tests) if (t.id.trim()) approved[t.id.trim()] = { argv: t.argv.trim().split(/\s+/).filter(Boolean), timeout_seconds: t.timeout };
    return { ...d0, mission, allowed_tools: tools, allowed_paths: lines(allowed), protected_paths: lines(protectedP),
      approved_tests: approved, limits, required_exports: lines(exportsTxt), network_access: false };
  }

  async function approveAndLaunch() {
    setBusy(true); setError(null);
    try {
      const c = created ?? await api.createContract({ draft: build(), approved: true, approved_by: approver });
      setCreated(c);
      const run = await api.createRun({ contract_id: c.contract_id, contract_version: c.version });
      // Ask the control plane to start the execution plane's runner. Failure here is NOT hidden: the
      // Active Run screen shows the backend's real launcher state and offers a manual launch.
      await api.launchAgent(run.run_id).catch(() => undefined);
      location.hash = `#/runs/${run.run_id}`;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Unexpected error");
    } finally { setBusy(false); }
  }

  const setLimit = (k: keyof typeof limits) => (e: React.ChangeEvent<HTMLInputElement>) => setLimits({ ...limits, [k]: num(e.target.value) });
  const toggle = (t: string) => setTools(tools.includes(t) ? tools.filter((x) => x !== t) : [...tools, t]);

  return (
    <>
      <Stepper active={1} />
      <Notice tone="info" title={state.source === "bedrock" ? `Draft proposed by Amazon Bedrock (attempt ${state.attempts ?? 1})` : "Draft loaded from the committed fixture (Bedrock bypassed)"}>
        This is a proposal only. It becomes enforceable when you approve it; the backend re-validates everything and enforces it deterministically.
      </Notice>
      <Card title="Mission">
        <label className="field"><span>Mission summary</span>
          <textarea rows={3} value={mission} onChange={(e) => setMission(e.target.value)} maxLength={2000} /></label>
        <p className="muted">Repository <code>{d0.repository_id}</code> · base commit <code>{d0.base_commit.slice(0, 12)}</code> · network access <Badge tone="deny">DISABLED (fixed)</Badge></p>
      </Card>
      <div className="grid2">
        <Card title="Allowed tools">
          {TOOLS.map((t) => (
            <label key={t} className="check"><input type="checkbox" checked={tools.includes(t)} onChange={() => toggle(t)} /><code>{t}</code></label>
          ))}
          <p className="muted">Also always available to an active run (no repository effect, still validated and counted): <code>request_permission</code>, <code>finish_task</code>.</p>
        </Card>
        <Card title="Required exports">
          <textarea rows={3} value={exportsTxt} onChange={(e) => setExportsTxt(e.target.value)} />
          <p className="muted">One per line. TwinGuard blocks a patch that removes one.</p>
        </Card>
        <Card title="Allowed paths"><textarea rows={5} value={allowed} onChange={(e) => setAllowed(e.target.value)} spellCheck={false} />
          <p className="muted">Globs, one per line, anchored at the repository root.</p></Card>
        <Card title="Protected paths"><textarea rows={5} value={protectedP} onChange={(e) => setProtectedP(e.target.value)} spellCheck={false} />
          <p className="muted">Take precedence over allowed paths. <code>.git</code> and <code>.env*</code> are always protected by the backend.</p></Card>
      </div>
      <Card title="Approved tests (fixed argv catalogue)">
        {tests.map((t, i) => (
          <div className="grid3" key={i}>
            <input aria-label="test id" value={t.id} onChange={(e) => setTests(tests.map((x, j) => j === i ? { ...x, id: e.target.value } : x))} placeholder="test-id" />
            <input aria-label="command" value={t.argv} onChange={(e) => setTests(tests.map((x, j) => j === i ? { ...x, argv: e.target.value } : x))} placeholder="npm run test:auth" />
            <span className="row"><input aria-label="timeout seconds" type="number" min={1} max={600} value={t.timeout} onChange={(e) => setTests(tests.map((x, j) => j === i ? { ...x, timeout: num(e.target.value) } : x))} /><span className="muted">s</span>
              <button className="linkbtn" onClick={() => setTests(tests.filter((_, j) => j !== i))}>remove</button></span>
          </div>
        ))}
        <button className="btn" onClick={() => setTests([...tests, { id: "", argv: "", timeout: 60 }])}>Add test</button>
        <p className="muted">Commands are split on whitespace into an argv array; no shell is ever used.</p>
      </Card>
      <Card title="Limits">
        <div className="grid3">
          {(Object.keys(limits) as (keyof typeof limits)[]).map((k) => (
            <label className="field" key={k}><span>{k.replaceAll("_", " ")}</span>
              <input type="number" min={0} value={limits[k]} onChange={setLimit(k)} /></label>
          ))}
        </div>
      </Card>
      <Card title="Approval">
        <label className="field"><span>Approving user</span><input value={approver} onChange={(e) => setApprover(e.target.value)} maxLength={128} /></label>
        <div className="row">
          <button className="btn primary" disabled={busy || !approver.trim() || tools.length === 0} onClick={approveAndLaunch}>
            {busy ? "Working…" : created ? "Retry launch" : "Approve and launch"}
          </button>
          <a className="btn" href="#/">Back</a>
        </div>
        {created && <p className="muted">Contract <code>{created.contract_id}</code> v{created.version} is active. Run creation failed; retry will reuse it.</p>}
        {error && <Notice tone="error" title="Could not approve / launch">{error}</Notice>}
        <p className="muted">Launching creates the control-plane run and asks the configured execution-plane runner to start the real CLI agent for it. The Active Run screen shows whether that actually happened.</p>
      </Card>
    </>
  );
}
