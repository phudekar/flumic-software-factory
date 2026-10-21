// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "./main";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
function app(path = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input.endsWith("/auth/session"))
        return json({ csrf: "csrf-fixture" });
      if (input.endsWith("/workers"))
        return json({
          managed: [],
          discovered: [],
          warnings: ["Docker observation unavailable."],
          observed_at: 0,
        });
      if (input.endsWith("/runs")) return json({ items: [], warnings: [] });
      if (input.endsWith("/usage"))
        return json({
          calls: 0,
          input_tokens: null,
          output_tokens: null,
          estimated_usd: null,
          reported_usd: null,
          unpriced_calls: 0,
          missing_usage_calls: 0,
        });
      if (input.endsWith("/config/revisions")) return json([]);
      if (input.includes("/events")) return json([]);
      return json({ detail: "Not found" }, 404);
    }),
  );
});
describe("control plane", () => {
  it("renders genuine empty and unavailable states without invented cost data", async () => {
    app();
    expect(
      await screen.findByRole("heading", { name: "Factory overview" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Docker observation unavailable."),
    ).toBeInTheDocument();
    expect(screen.getByText("No managed workers yet")).toBeInTheDocument();
    expect(screen.getByText("Unknown")).toBeInTheDocument();
  });
  it("asks for authentication before exposing operational data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({}, 401)),
    );
    app();
    expect(
      await screen.findByLabelText("Administrator password"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Managed workers")).not.toBeInTheDocument();
  });
  it("shows capture coverage and an honest empty conversation history", async () => {
    app("/history");
    expect(
      await screen.findByRole("heading", { name: "Model history" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("No model history captured yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Content expires after seven days/),
    ).toBeInTheDocument();
  });
  it("opens a stopped-worker form without triggering a start operation", async () => {
    app("/workers");
    fireEvent.click(
      await screen.findByRole("button", { name: "Create worker" }),
    );
    expect(
      screen.getByRole("button", { name: "Create stopped worker" }),
    ).toBeInTheDocument();
    const fetcher = vi.mocked(fetch);
    expect(
      fetcher.mock.calls.some((call) => String(call[0]).includes("/actions")),
    ).toBe(false);
  });
});
