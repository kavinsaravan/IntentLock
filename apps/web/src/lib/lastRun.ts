// Remembers the most recent run the user opened so the Mission and Contract screens, which have no run
// in their URL, can still link the workflow stepper to Agent activity, Impact review and Audit.
const KEY = "intentlock.lastRunId";
let memory: string | undefined;

function storage(): Storage | undefined {
  try { return typeof sessionStorage === "undefined" ? undefined : sessionStorage; } catch { return undefined; }
}

export function getLastRunId(): string | undefined {
  try { return storage()?.getItem(KEY) || memory; } catch { return memory; }
}

export function setLastRunId(runId: string): void {
  memory = runId;
  try { storage()?.setItem(KEY, runId); } catch { /* storage unavailable: in-memory value still works */ }
}

export function clearLastRunId(): void {
  memory = undefined;
  try { storage()?.removeItem(KEY); } catch { /* ignore */ }
}
