import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ChecksCard } from "./ImpactReview";

const test = (over: object = {}) => ({
  test_id: "payments-tests", status: "passed" as const, exit_code: 0, duration_ms: 120, output_s3_key: null, ...over,
});

describe("ChecksCard", () => {
  it("shows each selected check with the reason it was selected", () => {
    const html = renderToStaticMarkup(<ChecksCard r={{
      tests: [test({ reason: "matched src/payments/** (changed: src/payments/calculator.ts)" }), test({ test_id: "typecheck", reason: "always run" })],
      coverage: { selected_checks: ["payments-tests", "typecheck"], uncovered_paths: [] },
    }} />);
    expect(html).toContain("payments-tests");
    expect(html).toContain("matched src/payments/** (changed: src/payments/calculator.ts)");
    expect(html).toContain("typecheck");
    expect(html).not.toContain("Missing test coverage");
  });

  it("warns loudly about uncovered paths instead of implying they are safe", () => {
    const html = renderToStaticMarkup(<ChecksCard r={{
      tests: [test({ test_id: "typecheck" })],
      coverage: { selected_checks: ["typecheck"], uncovered_paths: ["src/utils/format.ts"] },
    }} />);
    expect(html).toContain("Missing test coverage");
    expect(html).toContain("src/utils/format.ts");
  });

  it("still renders legacy reports that have no reason or coverage", () => {
    const html = renderToStaticMarkup(<ChecksCard r={{ tests: [test({ test_id: "auth-tests" })] }} />);
    expect(html).toContain("auth-tests");
    expect(html).not.toContain("Missing test coverage");
  });

  it("says so when no checks ran", () => {
    expect(renderToStaticMarkup(<ChecksCard r={{ tests: [] }} />)).toContain("No checks were run");
  });
});
