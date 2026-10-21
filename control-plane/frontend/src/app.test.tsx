// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "./main";
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
  it("saves image build settings in a worker profile", async () => {
    fixtures();
    app("/config");
    await screen.findByLabelText("Image build YAML");
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue("echo ready"),
    );
    const build = screen.getByLabelText("Image build YAML");
    fireEvent.change(build, {
      target: {
        value: `# Build dependencies
base_image: python:3.12-slim
system_packages:
  - git
  - curl
codex_version: '0.163.0'
claude_version: '2.1.306'
image_setup_script: |-
  echo build prerequisites
  python -m pip --version
`,
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await screen.findByText(/Linked workers pick up profile edits/);
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([path, options]) =>
          String(path).endsWith("/worker-profiles") &&
          options?.method === "POST",
      );
    const settings = JSON.parse(String(call![1]!.body)).document.settings;
    expect(settings.base_image).toBe("python:3.12-slim");
    expect(settings.system_packages).toEqual(["git", "curl"]);
    expect(settings.image_setup_script).toBe(
      "echo build prerequisites\npython -m pip --version",
    );
    expect(settings.startup_script).toBe("echo ready");
  });
  it("blocks saving invalid image YAML instead of saving stale build settings", async () => {
    fixtures();
    app("/config");
    await screen.findByLabelText("Image build YAML");
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue("echo ready"),
    );
    const build = screen.getByLabelText("Image build YAML");
    for (const value of [
      "base_image: [",
      "unknown_option: true",
      "system_packages: git",
      "base_image: alpine\nbase_image: ubuntu",
      "base_image: &base alpine\nimage_setup_script: *base",
      "- base_image",
    ]) {
      fireEvent.change(build, { target: { value } });
      expect(
        screen.getByRole("button", { name: "Save revision" }),
      ).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Clone as new profile" }),
      ).toBeDisabled();
    }
    fireEvent.change(build, {
      target: { value: "base_image: python:3.12-slim" },
    });
    expect(screen.getByRole("button", { name: "Save revision" })).toBeEnabled();
  });
  it("renders full-width syntax-highlighted YAML while retaining editable textareas", async () => {
    fixtures();
    app("/config");
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue("echo ready"),
    );
    for (const label of ["Worker behavior YAML", "Image build YAML"]) {
      const editor = screen.getByLabelText(label);
      expect(editor.closest(".yaml-editor")).toHaveClass("yaml-editor");
      expect(
        editor.closest(".yaml-input")?.querySelector(".token.key"),
      ).not.toBeNull();
      expect(editor).toHaveAttribute("wrap", "off");
      fireEvent.scroll(editor, { target: { scrollTop: 72, scrollLeft: 24 } });
      expect(editor.closest(".yaml-input")?.querySelector("code")).toHaveStyle({
        transform: "translate(-24px, -72px)",
      });
    }
  });
  it("edits worker behavior as YAML and validates and formats both editors", async () => {
    fixtures();
    const original = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (input.endsWith("/worker-profiles/validate"))
          return json({ valid: true });
        return original(input, options);
      }),
    );
    app("/config");
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue("echo ready"),
    );
    const behavior = screen.getByLabelText("Worker behavior YAML");
    fireEvent.change(behavior, {
      target: {
        value:
          "# Keep this comment\nsettings: {openai_model: custom-model}\ntasks: {}\n",
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Format worker behavior YAML" }),
    );
    expect((behavior as HTMLTextAreaElement).value).toContain(
      "# Keep this comment",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Validate worker behavior YAML" }),
    );
    expect(
      await screen.findByText("Configuration is valid."),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Format image build YAML" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Validate image build YAML" }),
    );
    await waitFor(() =>
      expect(screen.getAllByText("Configuration is valid.")).toHaveLength(2),
    );
    fireEvent.change(behavior, { target: { value: "settings: [" } });
    expect(
      screen.getByRole("button", { name: "Save revision" }),
    ).toBeDisabled();
  });
  it("provides a full-width Markdown prompt editor with a safe preview", async () => {
    fixtures();
    app("/prompts");
    const prompt = await screen.findByLabelText("Additional system prompt");
    await waitFor(() => expect(prompt).toHaveValue("Plan carefully"));
    expect(prompt.closest(".markdown-prompt")).not.toBeNull();
    fireEvent.change(prompt, {
      target: {
        value:
          "# Acceptance criteria\n\n- **Keep** the API stable.\n\n<script>alert('unsafe')</script>",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview Markdown" }));
    expect(
      await screen.findByRole("heading", { name: "Acceptance criteria" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Keep", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(document.querySelector(".markdown-preview script")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Write Markdown" }));
    expect(
      (screen.getByLabelText("Additional system prompt") as HTMLTextAreaElement)
        .value,
    ).toContain("# Acceptance criteria");
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
  it("shows failed-attempt errors in parsed and raw log views without losing stage filters", async () => {
    fixtures();
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) => {
        if (input.includes("/engine/tasks/")) return json({ items: [] });
        if (
          input.includes("/events?") &&
          !input.includes("kind=attempt.started")
        )
          return json([
            {
              id: "failure-1",
              kind: "attempt.completed",
              worker_id: "worker-1",
              run_id: "run-1",
              attempt_id: "attempt-1",
              stage: "load_issue",
              occurred_at: 100,
              payload: {
                outcome: "failed",
                error_type: "RepositoryError",
                error_message:
                  "Selected source file exceeds the size limit: src/large.py",
              },
            },
          ]);
        return base(input, options);
      }),
    );
    app("/runs/run-1?task=task-1&attempt=attempt-1&tab=logs");
    expect(
      await screen.findByText(
        "RepositoryError: Selected source file exceeds the size limit: src/large.py",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Raw" }));
    expect(await screen.findByLabelText("Raw stage logs")).toHaveTextContent(
      '"error_message": "Selected source file exceeds the size limit: src/large.py"',
    );
    expect(screen.getByLabelText("Stage filter")).toHaveValue("task-1");
    expect(
      screen.queryByRole("columnheader", { name: "Timestamp" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Parsed" }));
    expect(
      await screen.findByRole("columnheader", { name: "Timestamp" }),
    ).toBeInTheDocument();
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
  it("shows unique profiles at latest version and opens older versions only through history", async () => {
    fixtures();
    const base = vi.mocked(fetch).getMockImplementation()!;
    const newest = {
      ...profile,
      id: "newest-config",
      parent_id: profile.id,
      created_at: 2,
      document: {
        ...profile.document,
        settings: {
          ...profile.document.settings,
          startup_script: "echo newest",
        },
      },
    };
    const other = {
      ...profile,
      id: "other-config",
      family_id: "other-family",
      name: "Reviewer worker",
      created_at: 3,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) =>
        input.endsWith("/worker-profiles") && options?.method !== "POST"
          ? json([profile, other, newest])
          : base(input, options),
      ),
    );
    app("/config");
    const selector = await screen.findByRole("combobox", {
      name: "Worker profile",
    });
    await waitFor(() =>
      expect(within(selector).getAllByRole("option")).toHaveLength(2),
    );
    expect(
      within(selector).getByRole("option", {
        name: "Backend worker · v2 (latest)",
      }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue(
        "echo newest",
      ),
    );
    expect(
      screen.queryByRole("button", { name: "View v1" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Version history" }));
    fireEvent.click(await screen.findByRole("button", { name: "View v1" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue("echo ready"),
    );
    expect(screen.getByLabelText("Startup script")).toHaveAttribute("readonly");
    expect(
      screen.getByRole("button", { name: "Save revision" }),
    ).toBeDisabled();
    fireEvent.click(
      screen.getByRole("button", { name: "Back to latest version" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Startup script")).toHaveValue(
        "echo newest",
      ),
    );
    expect(screen.getByRole("button", { name: "Save revision" })).toBeEnabled();
  });
  it("parses engine JSON messages using their own severity and preserves the raw record", async () => {
    fixtures();
    const base = vi.mocked(fetch).getMockImplementation()!;
    const structured = JSON.stringify({
      timestamp: "2026-10-21 05:00:50,732",
      level: "INFO",
      message: "Operation started",
      stage: "generate_and_open_draft_pr",
      attempt_id: "example-attempt",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options?: RequestInit) =>
        input.includes("/engine/tasks/")
          ? json({
              items: [
                {
                  createdAt: "2026-10-21T05:00:50.732Z",
                  level: "ERROR",
                  message: structured,
                },
                {
                  createdAt: "2026-10-21T05:00:51Z",
                  level: "ERROR",
                  message: "Plain engine error",
                },
              ],
            })
          : base(input, options),
      ),
    );
    app("/runs/run-1?task=task-1&tab=logs");
    expect(await screen.findByText("Operation started")).toBeInTheDocument();
    const row = screen.getByText("Operation started").closest("tr")!;
    expect(within(row).getByText("INFO")).toBeInTheDocument();
    expect(
      within(row).getByText("generate_and_open_draft_pr"),
    ).toBeInTheDocument();
    expect(screen.queryByText(structured)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Severity"), {
      target: { value: "ERROR" },
    });
    expect(screen.queryByText("Operation started")).not.toBeInTheDocument();
    expect(screen.getByText("Plain engine error")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Severity"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Raw" }));
    expect(await screen.findByLabelText("Raw stage logs")).toHaveTextContent(
      "example-attempt",
    );
    expect(screen.getByLabelText("Raw stage logs")).toHaveTextContent(
      '\\"level\\":\\"INFO\\"',
    );
  });
});
