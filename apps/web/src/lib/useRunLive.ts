import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { mergeEvents } from "./events";
import { EVENT_TYPES, type EventRec, type Run } from "./types";

export type Conn = "connecting" | "live" | "reconnecting" | "disconnected";

/**
 * Live view of one run. SSE is the primary channel (EventSource reconnects on its own and
 * resumes via Last-Event-ID). A 2s poll of the run + events reconciles state and keeps the
 * UI correct if SSE is blocked. The run object always comes from the backend.
 */
export function useRunLive(runId: string) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<EventRec[]>([]);
  const [conn, setConn] = useState<Conn>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const lastSeq = useRef(0);

  const absorb = useCallback((incoming: EventRec[]) => {
    if (incoming.length === 0) return;
    lastSeq.current = Math.max(lastSeq.current, ...incoming.map((e) => e.seq));
    setEvents((prev) => mergeEvents(prev, incoming));
  }, []);

  useEffect(() => { // reset when switching runs
    setRun(null); setEvents([]); setNotFound(false); setError(null); lastSeq.current = 0;
  }, [runId]);

  useEffect(() => { // SSE
    setConn("connecting");
    const es = new EventSource(api.streamUrl(runId, lastSeq.current));
    es.onopen = () => setConn("live");
    es.onerror = () => setConn(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
    const handler = (m: MessageEvent) => {
      try { absorb([JSON.parse(m.data) as EventRec]); } catch { /* ignore malformed frame */ }
    };
    EVENT_TYPES.forEach((t) => es.addEventListener(t, handler as EventListener));
    return () => es.close();
  }, [runId, retryKey, absorb]);

  useEffect(() => { // reconcile poll
    let cancelled = false;
    const tick = async () => {
      try {
        const [r, e] = await Promise.all([api.getRun(runId), api.getEvents(runId, 0)]);
        if (cancelled) return;
        setRun(r); absorb(e.events); setError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) setNotFound(true);
        else setError(err instanceof ApiError ? err.message : "poll failed");
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [runId, absorb]);

  return { run, setRun, events, conn, error, notFound, reconnect: () => setRetryKey((k) => k + 1) };
}
