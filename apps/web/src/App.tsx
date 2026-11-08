import { useEffect, useState } from "react";
import { Badge } from "./components/ui";
import { api } from "./lib/api";
import { parseReplayHash } from "./lib/replay";
import type { Health } from "./lib/types";
import { ActiveRun } from "./screens/ActiveRun";
import { ApprovedContract } from "./screens/ApprovedContract";
import { CreateMission, type DraftState } from "./screens/CreateMission";
import { FinalAudit } from "./screens/FinalAudit";
import { ImpactReview } from "./screens/ImpactReview";
import { ReviewContract } from "./screens/ReviewContract";
import { SecurityReplay } from "./screens/SecurityReplay";

function useHash() {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => { const f = () => setH(location.hash || "#/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h;
}

function Header() {
  const [health, setHealth] = useState<Health | null | "down">(null);
  useEffect(() => {
    let off = false;
    const poll = () => api.health().then((h) => !off && setHealth(h)).catch(() => !off && setHealth("down"));
    poll(); const id = setInterval(poll, 5000);
    return () => { off = true; clearInterval(id); };
  }, []);
  return (
    <header className="top">
      <a className="brand" href="#/"><b>IntentLock</b> <span>with TwinGuard</span></a>
      <nav className="nav"><a href="#/">New mission</a><a href="#/replay">Security Replay</a></nav>
      <span className="env">
        {health === null && <Badge tone="pending">checking API…</Badge>}
        {health === "down" && <Badge tone="deny">API unreachable</Badge>}
        {health && health !== "down" && <><Badge tone="ok">API connected</Badge> <Badge tone={health.store.includes("NOT") ? "review" : "info"}>store: {health.store}</Badge> <Badge tone="info">schema {health.schema_version}</Badge></>}
      </span>
    </header>
  );
}

export default function App() {
  const hash = useHash();
  const [draft, setDraft] = useState<DraftState | null>(null);
  const m = /^#\/runs\/([^/]+)(?:\/(contract|impact|audit))?$/.exec(hash);
  const replay = parseReplayHash(hash);
  let screen;
  if (replay) {
    screen = <SecurityReplay runId={replay.runId} source={replay.source} />;
  } else if (m) {
    const id = decodeURIComponent(m[1]);
    screen = m[2] === "contract" ? <ApprovedContract key={id} runId={id} />
      : m[2] === "impact" ? <ImpactReview key={id} runId={id} />
      : m[2] === "audit" ? <FinalAudit key={id} runId={id} />
      : <ActiveRun key={id} runId={id} />;
  } else if (hash === "#/review") screen = <ReviewContract state={draft} />;
  else screen = <CreateMission onDraft={setDraft} />;
  return (<><Header /><main>{screen}</main></>);
}
