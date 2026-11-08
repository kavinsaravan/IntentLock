import { useState } from "react";
import { Card, Notice, Spinner, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { getLastRunId } from "../lib/lastRun";
import type { ContractDraft } from "../lib/types";
import fixtureDraft from "../../../../packages/contracts/fixtures/contract-draft.json";

export interface DraftState { draft: ContractDraft; source: "bedrock" | "fixture"; attempts?: number }
const COMMIT_RX = /^[0-9a-f]{40}$/;
const DEFAULT_TASK = "Read src/auth/session.ts, then propose a patch fixing the timeout's seconds-to-milliseconds conversion. TwinGuard runs auth-tests on the patch; finish after its impact report. Do not inspect or edit tests, secrets, or the network.";

export function CreateMission({ onDraft }: { onDraft: (d: DraftState) => void }) {
  const [task, setTask] = useState(DEFAULT_TASK);
  const [commit, setCommit] = useState((import.meta.env.VITE_DEMO_BASE_COMMIT as string | undefined) ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const commitOk = COMMIT_RX.test(commit);

  async function generate() {
    setBusy(true); setError(null);
    try {
      const r = await api.propose({ task, repository_id: "intentlock-demo", base_commit: commit });
      onDraft({ draft: r.draft, source: "bedrock", attempts: r.attempts });
      location.hash = "#/review";
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Unexpected error");
    } finally { setBusy(false); }
  }

  // PRD §23 fallback: prevalidated fixture. Clearly labelled; still requires human approval + validation.
  function useFixture() {
    const d = { ...(fixtureDraft as unknown as ContractDraft), mission: task.slice(0, 2000) || (fixtureDraft as { mission: string }).mission, base_commit: commit };
    onDraft({ draft: d, source: "fixture" });
    location.hash = "#/review";
  }

  return (
    <>
      <Stepper active={0} runId={getLastRunId()} />
      <Card title="Create mission">
        <label className="field">
          <span>Task for the coding agent</span>
          <textarea rows={5} value={task} onChange={(e) => setTask(e.target.value)} disabled={busy} maxLength={2000} />
        </label>
        <div className="grid2">
          <label className="field">
            <span>Repository</span>
            <input value="intentlock-demo" disabled />
          </label>
          <label className="field">
            <span>Base commit (40-hex, recorded Git SHA)</span>
            <input value={commit} onChange={(e) => setCommit(e.target.value.trim().toLowerCase())} placeholder="0123…" spellCheck={false} disabled={busy} />
            {commit && !commitOk && <small className="bad">Must be a 40-character hex SHA.</small>}
          </label>
        </div>
        <div className="row">
          <button className="btn primary" onClick={generate} disabled={busy || !task.trim() || !commitOk}>
            {busy ? "Generating…" : "Generate contract"}
          </button>
          {busy && <Spinner label="Asking Amazon Bedrock for a draft contract (not yet active)…" />}
        </div>
        {error && (
          <Notice tone="error" title="Contract proposal failed">
            {error}. No contract was created. If Bedrock is not configured you can continue with the prevalidated fixture draft below.
          </Notice>
        )}
        <hr />
        <p className="muted">
          Bedrock only <em>proposes</em>. You review and edit the draft next, and nothing is enforced until you approve it.
        </p>
        <button className="btn" onClick={useFixture} disabled={busy || !commitOk} title="Bypasses Bedrock. Uses packages/contracts/fixtures/contract-draft.json">
          Use fixture draft instead (Bedrock bypassed)
        </button>
      </Card>
    </>
  );
}
