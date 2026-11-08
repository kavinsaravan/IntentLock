import { useEffect, useState } from "react";
import { Badge, Card, Notice, Spinner, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import type { Contract } from "../lib/types";

/** The contract bound to an existing run, displayed without draft editing controls. */
export function ApprovedContract({ runId }: { runId: string }) {
  const [contract, setContract] = useState<Contract | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setContract(null); setError(null);
    api.getRun(runId)
      .then((run) => api.getContract(run.contract_id, run.contract_version))
      .then((value) => { if (!cancelled) setContract(value); })
      .catch((e: unknown) => { if (!cancelled) setError(e instanceof ApiError ? e.message : "Could not load contract"); });
    return () => { cancelled = true; };
  }, [runId]);

  return <>
    <Stepper active={1} runId={runId} />
    {error && <Notice tone="error" title="Contract unavailable">{error}. <a href={`#/runs/${encodeURIComponent(runId)}`}>Return to agent activity</a>.</Notice>}
    {!error && !contract && <Spinner label="Loading the contract for this run…" />}
    {contract && <>
      <Notice tone="info" title="Approved contract for this run">
        This is the recorded contract, not an editable draft. Return to <a href={`#/runs/${encodeURIComponent(runId)}`}>Agent activity</a> to follow the run.
      </Notice>
      <Card title="Mission">
        <p>{contract.mission}</p>
        <p className="muted">Contract <code>{contract.contract_id}</code> · version {contract.version} · approved by <b>{contract.created_by}</b> · status <Badge tone={contract.status === "active" ? "ok" : "review"}>{contract.status}</Badge></p>
        <p className="muted">Repository <code>{contract.repository_id}</code> · base commit <code>{contract.base_commit}</code> · network access <Badge tone="deny">DISABLED</Badge></p>
      </Card>
      <div className="grid2">
        <Card title="Allowed tools"><ul>{contract.allowed_tools.map((tool) => <li key={tool}><code>{tool}</code></li>)}</ul></Card>
        <Card title="Required exports"><ul>{contract.required_exports.map((name) => <li key={name}><code>{name}</code></li>)}</ul></Card>
        <Card title="Allowed paths"><ul>{contract.allowed_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul></Card>
        <Card title="Protected paths"><ul>{contract.protected_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul></Card>
      </div>
      <Card title="Approved tests">
        <ul>{Object.entries(contract.approved_tests).map(([id, test]) =>
          <li key={id}><code>{id}</code>: <code>{test.argv.join(" ")}</code> · {test.timeout_seconds}s limit</li>)}</ul>
      </Card>
      <Card title="Limits">
        <ul>{Object.entries(contract.limits).map(([name, value]) =>
          <li key={name}>{name.replaceAll("_", " ")}: <b>{value}</b></li>)}</ul>
      </Card>
      <p><a className="btn primary" href={`#/runs/${encodeURIComponent(runId)}`}>Back to agent activity →</a></p>
    </>}
  </>;
}
