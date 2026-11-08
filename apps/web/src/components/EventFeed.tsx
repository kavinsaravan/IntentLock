import { useState } from "react";
import { fmtMs, fmtTime } from "../lib/format";
import type { EventRec } from "../lib/types";
import { Badge } from "./ui";

function Row({ e }: { e: EventRec }) {
  const [open, setOpen] = useState(false);
  const isTool = e.event_type === "tool_request";
  const tone = !isTool ? (e.event_type === "run_stopped" ? "stopped" : "info")
    : e.decision === "allow" ? "allow" : e.decision === "deny" ? "deny" : "pending";
  const label = !isTool ? e.event_type.replace("_", " ").toUpperCase()
    : e.decision === "allow" ? "ALLOWED" : e.decision === "deny" ? "DENIED" : "EVALUATING";
  const hasMore = e.evidence || e.details || e.arguments_redacted;
  return (
    <li className={`ev ev-${tone}`}>
      <div className="ev-main">
        <span className="ev-time">{fmtTime(e.timestamp)}</span>
        <Badge tone={tone}>{label}</Badge>
        {isTool && <code className="ev-tool">{e.tool ?? "?"}</code>}
        {isTool && e.arguments_redacted && <code className="ev-args">{JSON.stringify(e.arguments_redacted)}</code>}
        {e.reason_code && <code className={`ev-reason ${e.decision === "deny" ? "bad" : ""}`}>{e.reason_code}</code>}
        {e.latency_ms !== null && <span className="ev-lat">{fmtMs(e.latency_ms)}</span>}
        <span className="ev-seq">#{e.seq}</span>
      </div>
      {e.message && <div className="ev-msg">{e.message}</div>}
      {hasMore && (
        <button className="linkbtn" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "hide evidence" : "show evidence"}
        </button>
      )}
      {open && <pre className="json">{JSON.stringify({ evidence: e.evidence, details: e.details, request_id: e.request_id }, null, 2)}</pre>}
    </li>
  );
}

export function EventFeed({ events }: { events: EventRec[] }) {
  if (events.length === 0) return <p className="muted">No events yet. Events appear here as the execution plane sends tool requests to /internal/authorize.</p>;
  return <ol className="feed">{events.map((e) => <Row key={e.seq} e={e} />)}</ol>;
}
