import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { clearLastRunId, setLastRunId } from "../lib/lastRun";
import { Stepper } from "./ui";

afterEach(() => { vi.unstubAllGlobals(); clearLastRunId(); });

describe("run navigation", () => {
  it("keeps the run ID when moving between agent activity and contract", () => {
    const fromAgent = renderToStaticMarkup(<Stepper active={2} runId="run_123" />);
    expect(fromAgent).toContain('href="#/runs/run_123/contract"');
    const fromContract = renderToStaticMarkup(<Stepper active={1} runId="run_123" />);
    expect(fromContract).toContain('href="#/runs/run_123"');
  });

  it("opens the approved contract page for an existing run", () => {
    vi.stubGlobal("location", { hash: "#/runs/run_123/contract" });
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("Loading the contract for this run");
    expect(html).toContain('href="#/runs/run_123"');
  });

  it("keeps later steps linked on the Mission page once a run exists", () => {
    vi.stubGlobal("location", { hash: "#/" });
    const before = renderToStaticMarkup(<App />);
    expect(before).not.toContain("/impact");
    expect(before).not.toContain("/audit");

    setLastRunId("run_123");
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('href="#/runs/run_123"');
    expect(html).toContain('href="#/runs/run_123/impact"');
    expect(html).toContain('href="#/runs/run_123/audit"');
  });

  it("keeps later steps linked on the Contract review page once a run exists", () => {
    setLastRunId("run_123");
    vi.stubGlobal("location", { hash: "#/review" });
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("No draft to review");
    expect(html).toContain('href="#/runs/run_123/impact"');
    expect(html).toContain('href="#/runs/run_123/audit"');
  });

  it("remembers a run opened from the URL for later Mission visits", () => {
    vi.stubGlobal("location", { hash: "#/runs/run_456/audit" });
    renderToStaticMarkup(<App />);
    vi.stubGlobal("location", { hash: "#/" });
    expect(renderToStaticMarkup(<App />)).toContain('href="#/runs/run_456/impact"');
  });
});
