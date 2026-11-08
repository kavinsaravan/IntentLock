import { useEffect, useState } from "react";
import { EventFeed } from "../components/EventFeed";
import { Badge, Card, Notice, Spinner, StatusPill, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { countDecisions } from "../lib/events";
import { elapsedSeconds } from "../lib/format";
import { TERMINAL, type Contract, type LauncherInfo, type LaunchStatus } from "../lib/types";
import { useRunLive } from "../lib/useRunLive";

const CONN_TEXT = {
  connecting: "Connecting to live stream…", live: "Live (SSE)",
  reconnecting: "Stream interrupted — reconnecting (polling continues)", disconnected: "Stream closed — polling every 2s",
} as const;

const TERMINATION_TEXT: Record<string, string> = {
  launcher_watcher_armed: "the control plane's launcher watcher is armed to terminate the runner if it does not exit on its own",
  launcher_already_exited: "the launcher had already exited",
  delegated_to_execution_plane: "no launcher was started by this control plane; the execution plane must terminate its agent",
};

function useLaunch(runId: string) {
  const [info, setInfo] = useState<LauncherInfo | null>(null);
  const [status, setStatus] = useState<LaunchStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.launcherInfo().then(setInfo).catch((e) => setErr(e instanceof ApiError ? e.message : "launcher info failed")); }, []);
  useEffect(() => {
    let off = false;
    const tick = () => api.launchStatus(runId).then((s) => !off && setStatus(s)).catch(() => undefined);
    tick(); const id = setInterval(tick, 2000);
    return () => { off = true; clearInterval(id); };
  }, [runId]);
  const [busy, setBusy] = useState(false);
  const launch = async () => {
    setBusy(true); setErr(null);
    try { await api.launchAgent(runId); setStatus(await api.launchStatus(runId)); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "launch failed"); }
    finally { setBusy(false); }
  };
  return { info, status, err, busy, launch };
}

export function ActiveRun({ runId }: { runId: string }) {
  const { run, setRun, events, conn, error, notFound, reconnect } = useRunLive(runId);
  const lc = useLaunch(runId);
  const [contract, setContract] = useState<Contract | null>(null);
  const [stopInfo, setStopInfo] = useState<string | null>(null);
  const [stopErr, setStopErr] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [tick, setTick] = useState(Date.now());

  useEffect(() => { const id = setInterval(() => setTick(Date.now()), 1000); return () => clearInterval(id); }, []);
  useEffect(() => {
    if (run && !contract) api.getContract(run.contract_id, run.contract_version).then(setContract).catch(() => undefined);
  }, [run, contract]);

  async function stop() {
    setStopping(true); setStopErr(null);
    try {
      const r = await api.stopRun(runId, "stopped by user from dashboard");
      setRun(r.run); // authoritative state returned by the backend
      setStopInfo(r.state_committed
        ? `Run state committed as "${r.run.status}". Agent termination: ${TERMINATION_TEXT[r.process_termination] ?? r.process_termination}.`
        : "Backend did not confirm the state change.");
    } catch (e) { setStopErr(e instanceof ApiError ? e.message : "Stop failed"); }
    finally { setStopping(false); }
  }

  if (notFound) return (<><Stepper active={2} /><Notice tone="error" title="Run not found">No run with id <code>{runId}</code> exists on this backend (the local in-memory store resets on restart).</Notice></>);
  const counts = countDecisions(events);
  const terminal = run ? TERMINAL.includes(run.status) : false;
  const elapsed = run ? elapsedSeconds(run.started_at, run.stopped_at ?? run.completed_at, tick) : null;
  const limit = contract?.limits.run_timeout_seconds;

  return (
    <>
      <Stepper active={2} runId={runId} />
      {error && <Notice tone="warn" title="Backend unreachable or erroring">{error}. Showing last known state.</Notice>}
      <Card title="Run" right={run && <StatusPill status={run.status} />}>
        {!run ? <Spinner label="Loading run from backend…" /> : (
          <>
            <dl className="kv">
              <dt>Run</dt><dd><code>{run.run_id}</code></dd>
              <dt>Contract</dt><dd><code>{run.contract_id}</code> · version {run.contract_version}{contract && contract.status !== "active" && <> <Badge tone="stopped">{contract.status}</Badge></>}</dd>
              <dt>Agent process</dt><dd>{run.agent_process_id ? <code>{run.agent_process_id}</code> : <span className="muted">not reported — the execution plane has not attached an agent yet</span>}</dd>
              <dt>Agent launcher</dt><dd>{
                lc.status?.launched
                  ? <>{lc.status.alive ? <Badge tone="allow">RUNNING</Badge> : <Badge tone="stopped">EXITED{lc.status.exit_code !== null && lc.status.exit_code !== undefined ? ` (code ${lc.status.exit_code})` : ""}</Badge>} <code>pid {lc.status.launcher_pid}</code>{lc.status.escalation && lc.status.escalation.length > 0 && <span className="muted"> · backstop signals sent: {lc.status.escalation.join(" → ")}</span>}</>
                  : lc.info && !lc.info.configured
                    ? <span className="muted">not configured on this backend{lc.info.detail ? ` (${lc.info.detail})` : ""}; start the execution-plane runner manually</span>
                    : lc.info?.configured && !terminal
                      ? <><span className="muted">not launched </span><button className="btn" onClick={lc.launch} disabled={lc.busy}>{lc.busy ? "Launching…" : "Launch agent"}</button></>
                      : <span className="muted">not launched</span>}
              </dd>
              <dt>Sandbox</dt><dd>{run.sandbox_id ? <code>{run.sandbox_id}</code> : <span className="muted">none reported</span>}</dd>
              <dt>Elapsed</dt><dd>{elapsed !== null ? `${elapsed}s` : "—"}{limit ? ` of ${limit}s limit` : ""}</dd>
              {run.stop_reason && (<><dt>Stop reason</dt><dd><code>{run.stop_reason}</code></dd></>)}
            </dl>
            <div className="row">
              <button className="btn danger" onClick={stop} disabled={stopping || terminal}>{stopping ? "Stopping…" : terminal ? "Run is not active" : "Stop run"}</button>
              <a className="btn" href={`#/runs/${runId}/impact`}>Impact Preview →</a>
              <a className="btn" href={`#/runs/${runId}/audit`}>Final audit →</a>
            </div>
            {stopInfo && <Notice tone="info">{stopInfo}</Notice>}
            {stopErr && <Notice tone="error" title="Stop request failed">{stopErr}</Notice>}
            {lc.err && <Notice tone="warn" title="Agent launcher">{lc.err}</Notice>}
            {run.status === "blocked" && <Notice tone="error" title="Run blocked by IntentLock">A request was denied with a stop-run decision. All later requests are rejected as RUN_NOT_ACTIVE.</Notice>}
          </>
        )}
      </Card>
      <Card title="Live activity" right={
        <span className={`conn conn-${conn}`}>{CONN_TEXT[conn]}{conn === "disconnected" && <> <button className="linkbtn" onClick={reconnect}>retry stream</button></>}</span>}>
        <p className="counts"><Badge tone="allow">{counts.allowed} allowed</Badge> <Badge tone="deny">{counts.denied} denied</Badge>{counts.pending > 0 && <> <Badge tone="pending">{counts.pending} evaluating</Badge></>}</p>
        <EventFeed events={events} />
      </Card>
    </>
  );
}
