import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { Stepper } from "./ui";

afterEach(() => vi.unstubAllGlobals());

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
});
