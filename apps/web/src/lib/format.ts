export const shortSha = (s: string, n = 8) => s.slice(0, n);
export function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}
export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms < 10 ? `${ms.toFixed(2)} ms` : `${ms.toFixed(1)} ms`;
}
export const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
export function elapsedSeconds(startIso: string | null, endIso: string | null, nowMs: number): number | null {
  if (!startIso) return null;
  const end = endIso ? new Date(endIso).getTime() : nowMs;
  return Math.max(0, Math.round((end - new Date(startIso).getTime()) / 1000));
}
