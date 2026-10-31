// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "./main";
import { api } from "./api";
import { RuntimeLogView } from "./components/logs";

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
});

describe("request retries", () => {
  it("preserves explicit idempotency identity after an uncertain worker action", async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Network unavailable"))
      .mockResolvedValueOnce(json({ id: "operation", status: "completed" }));
    vi.stubGlobal("fetch", fetcher);
    const key = "same-request-identity";
    await expect(
      api(
        "/workers/shared/actions",
        { action: "restart" },
        { idempotencyKey: key },
      ),
    ).rejects.toThrow("Network unavailable");
    await api(
      "/workers/shared/actions",
      { action: "restart" },
      { idempotencyKey: key },
    );
    expect(fetcher.mock.calls[0][1].headers).toEqual(
      fetcher.mock.calls[1][1].headers,
    );
    expect(fetcher.mock.calls[0][1].body).toEqual(
      fetcher.mock.calls[1][1].body,
    );
  });
});

describe("shared worker observation", () => {
  const profile = {
    id: "config-1",
    family_id: "profile-1",
    parent_id: null,
    cloned_from: null,
    name: "Backend worker",
    profile: "simple",
    factory_revision_id: "factory-1",
    document: {
      settings: {
        openai_model: "fixture-model",
        startup_script: "echo ready",
        startup_timeout_seconds: 60,
      },
      tasks: {
        plan_issue: { model: null, additional_system_prompt: "Plan carefully" },
      },
    },
    created_at: 1,
  };
  function fixtures() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (input.endsWith("/auth/session"))
          return json({ csrf: "fixture-csrf" });
        if (input.endsWith("/worker-profiles"))
          return json(
            options?.method === "POST"
              ? { ...profile, id: "config-2" }
              : [profile],
            options?.method === "POST" ? 201 : 200,
          );
        if (input.includes("/worker-configs/template/")) return json(profile);
        if (input.endsWith("/factory-config"))
          return json({
            id: "factory-1",
            document: {
              profiles: {
                simple: { settings: { scan_cron: "*/5 * * * *" }, tasks: {} },
              },
            },
          });
        if (input.endsWith("/config/revisions"))
          return json([{ id: "factory-1", summary: "Shared settings" }]);
        if (input.endsWith("/workers"))
          return json(
            options?.method === "POST"
              ? { id: "op-1" }
              : { managed: [], discovered: [], warnings: [] },
          );
        if (input.includes("/usage?"))
          return json({
            calls: 1,
            input_tokens: 123,
            output_tokens: 45,
            estimated_usd: 0.1,
            reported_usd: null,
            unpriced_calls: 0,
            missing_usage_calls: 0,
          });
        if (input.endsWith("/runs/run-1"))
          return json({
            run: { id: "run-1", name: "Test workflow", status: "RUNNING" },
            tasks: [
              { id: "task-1", name: "Plan", status: "COMPLETED" },
              { id: "task-2", name: "Code", status: "RUNNING" },
            ],
          });
        if (input.includes("/events?")) return json([]);
        return json({}, 404);
      }),
    );
  }
  it("hides exited discovered workers until requested", async () => {
    fixtures();
    const original = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (input.endsWith("/workers") && options?.method !== "POST")
          return json({
            managed: [],
            discovered: [
              {
                id: "running-container",
                name: "running-compose-worker",
                status: "running",
                image: "worker:latest",
              },
              {
                id: "exited-container",
                name: "exited-compose-worker",
                status: "exited",
                image: "worker:latest",
              },
            ],
            warnings: [],
          });
        return original(input, options);
      }),
    );
    app("/workers");
    expect(
      await screen.findByText("running-compose-worker"),
    ).toBeInTheDocument();
    expect(screen.queryByText("exited-compose-worker")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Show exited discovered workers"));
    expect(
      await screen.findByText("exited-compose-worker"),
    ).toBeInTheDocument();
  });
  it("hides removed workers by default and exposes their history on request", async () => {
    fixtures();
    const original = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (input.endsWith("/workers") && options?.method !== "POST")
          return json({
            managed: [
              {
                id: "removed-worker",
                name: "retired-worker",
                profile: "simple",
                revision_id: "revision-old",
                desired_state: "removed",
                runtime: null,
                engine: null,
                operations: [],
              },
            ],
            discovered: [],
            warnings: [],
          });
        return original(input, options);
      }),
    );
    app("/workers");
    await screen.findByRole("heading", { name: "Your fleet starts here" });
    expect(screen.queryByText("retired-worker")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Show removed workers"));
    expect(await screen.findByText("retired-worker")).toBeInTheDocument();
    expect(screen.getByTitle("Start worker")).toBeDisabled();
  });
  it("shows build logs before a worker container exists", async () => {
    fixtures();
    const original = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (input.endsWith("/workers/worker-1/build"))
          return json({
            status: "building",
            image: "factory:pending",
            content: "Installing worker dependencies",
          });
        return original(input, options);
      }),
    );
    app("/workers/worker-1");
    expect(
      await screen.findByText("Installing worker dependencies"),
    ).toBeInTheDocument();
    expect(screen.getByText("building")).toBeInTheDocument();
    expect(screen.getByText("factory:pending")).toBeInTheDocument();
  });
  it("preserves structured and plain runtime lines when toggling raw output", () => {
    const content =
      JSON.stringify({
        timestamp: "2026-10-20 23:00:00",
        level: "ERROR",
        stage: "load_issue",
        message: "Source file exceeds the size limit",
      }) + "\nPlain traceback continuation";
    render(<RuntimeLogView content={content} />);
    expect(
      screen.getByText("Source file exceeds the size limit"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Plain traceback continuation"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Raw" }));
    expect(screen.getByLabelText("Raw runtime logs").textContent).toBe(content);
  });
});
