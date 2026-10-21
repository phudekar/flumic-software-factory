// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  render,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
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

describe("worker profiles and scoped execution", () => {
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
  it("creates workers from a saved profile identity", async () => {
    fixtures();
    app("/workers");
    fireEvent.click(
      await screen.findByRole("button", { name: "Create worker" }),
    );
    expect(
      await screen.findByRole("option", { name: "Backend worker · simple" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Instance name"), {
      target: { value: "new-worker" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Create stopped worker" }),
    );
    await screen.findByRole("heading", { name: "Your fleet starts here" });
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([path, options]) =>
          String(path).endsWith("/workers") && options?.method === "POST",
      );
    expect(call).toBeDefined();
    expect(JSON.parse(String(call![1]!.body))).toEqual({
      name: "new-worker",
      worker_profile_id: "profile-1",
      target_id: "local-docker",
    });
  });
  it("saves a startup script on the selected profile without a worker action", async () => {
    fixtures();
    app("/config");
    const script = await screen.findByLabelText("Startup script");
    await waitFor(() => expect(script).toHaveValue("echo ready"));
    fireEvent.change(script, {
      target: { value: "echo install prerequisites" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    expect(
      await screen.findByText(/Linked workers pick up profile edits/),
    ).toBeInTheDocument();
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([path, options]) =>
          String(path).endsWith("/worker-profiles") &&
          options?.method === "POST",
      );
    const payload = JSON.parse(String(call![1]!.body));
    expect(payload.parent_id).toBe("config-1");
    expect(payload.document.settings.startup_script).toBe(
      "echo install prerequisites",
    );
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([path]) => String(path).includes("/actions")),
    ).toBe(false);
  });
  it("filters both event history and accounting when selecting a stage", async () => {
    fixtures();
    app("/runs/run-1");
    fireEvent.click(await screen.findByRole("button", { name: /Plan/ }));
    await screen.findByText("123 / 45");
    const paths = vi.mocked(fetch).mock.calls.map(([path]) => String(path));
    expect(
      paths.some(
        (path) =>
          path.includes("/usage?") && path.includes("engine_task_id=task-1"),
      ),
    ).toBe(true);
    expect(
      paths.some(
        (path) =>
          path.includes("/events?") && path.includes("engine_task_id=task-1"),
      ),
    ).toBe(true);
  });
  it("distinguishes input and output without loading conversation content until requested", async () => {
    fixtures();
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (
          input.includes("/events?") &&
          !input.includes("kind=attempt.started")
        )
          return json([
            {
              id: "call-1",
              kind: "model.completed",
              worker_id: "worker-1",
              run_id: "run-1",
              attempt_id: "attempt-1",
              stage: "plan_issue",
              occurred_at: 100,
              payload: {
                model: "fixture-model",
                outcome: "completed",
                conversation_available: true,
              },
            },
          ]);
        if (input.endsWith("/model-calls/call-1/conversation"))
          return json({
            request: "Redacted worker instructions",
            response: "Redacted model completion",
            coverage: "api_exchange",
            request_at: 99,
            response_at: 100,
          });
        return base(input, options);
      }),
    );
    app("/runs/run-1?task=task-1&attempt=attempt-1");
    fireEvent.click(await screen.findByRole("button", { name: "logs" }));
    const expand = await screen.findByRole("button", {
      name: "Inspect worker prompt and model output",
    });
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([path]) => String(path).includes("/conversation")),
    ).toBe(false);
    expect(
      screen.getByRole("columnheader", { name: "Timestamp" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Level" }),
    ).toBeInTheDocument();
    fireEvent.click(expand);
    expect(
      await screen.findByText("Redacted worker instructions"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Worker input prompt" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Model output" }),
    ).toBeInTheDocument();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([path]) => String(path).includes("/engine/tasks/")),
    ).toBe(false);
  });
  it("opens stage-filtered logs from a stage card and keeps logs open when changing stages", async () => {
    fixtures();
    app("/runs/run-1");
    fireEvent.click(
      await screen.findByRole("link", { name: "View logs for Plan" }),
    );
    expect(
      await screen.findByRole("heading", {
        name: "Stage logs and model exchanges",
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Stage filter")).toHaveValue("task-1");
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(([path]) =>
            String(path).includes("/engine/tasks/task-1/logs"),
          ),
      ).toBe(true),
    );
    fireEvent.change(screen.getByLabelText("Stage filter"), {
      target: { value: "task-2" },
    });
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([path]) =>
              String(path).includes("/events?") &&
              String(path).includes("engine_task_id=task-2"),
          ),
      ).toBe(true),
    );
    expect(
      screen.getByRole("heading", { name: "Stage logs and model exchanges" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "All stages and attempts" }),
    );
    expect(screen.getByLabelText("Stage filter")).toHaveValue("");
    expect(
      screen.getByRole("heading", { name: "Stage logs and model exchanges" }),
    ).toBeInTheDocument();
  });
  it("restores the logs tab and stage/attempt scope from a direct URL", async () => {
    fixtures();
    app("/runs/run-1?task=task-1&attempt=attempt-1&worker=worker-1&tab=logs");
    expect(
      await screen.findByRole("heading", {
        name: "Stage logs and model exchanges",
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Stage filter")).toHaveValue("task-1");
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([path]) => {
          const url = new URL(String(path), "http://localhost");
          return (
            url.pathname.endsWith("/events") &&
            url.searchParams.get("engine_task_id") === "task-1" &&
            url.searchParams.get("attempt_id") === "attempt-1" &&
            url.searchParams.get("worker_id") === "worker-1"
          );
        }),
      ).toBe(true),
    );
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([path]) => String(path).includes("/engine/tasks/")),
    ).toBe(false);
  });
});
