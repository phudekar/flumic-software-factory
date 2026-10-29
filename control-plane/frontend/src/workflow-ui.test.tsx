// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { App } from "./app/App";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const graph = {
  schema_version: 1,
  name: "mixed-check",
  repositories: ["backend"],
  default_repository: "backend",
  stages: [
    {
      id: "load",
      executor: "builtin",
      action: "core.echo@1",
      role: "simple",
      pool_id: "pool",
      after: [],
      inputs: { message: { $ref: "input.message" } },
    },
    {
      id: "check",
      executor: "docker",
      role: "simple",
      pool_id: "pool",
      after: ["load"],
      image: "example/check@sha256:" + "a".repeat(64),
      command: ["python", "check.py"],
      inputs: { value: { $ref: "stages.load.output" } },
    },
  ],
};
const stages = graph.stages.map((s) => ({
  ...s,
  executor: s.executor + "@1",
  repository_id: "repo",
  repository_alias: "backend",
  repository_revision_id: "repo-r1",
  config_revision_id: "config-r1",
  settings: { openai_model: "pinned-model" },
}));
const receipt = {
  id: "run-1",
  project_id: "a",
  definition_id: "def",
  name: "mixed-check",
  revision_id: "r1",
  state: "queued",
  snapshot_id: "pin",
  created_at: 1,
  dispatch: { state: "pending", engine_id: null, error: null },
  default_repository: "backend",
  factory_revision_id: "config-r1",
  repositories: { backend: { id: "repo", revision_id: "repo-r1" } },
  stages,
  attempts: [
    {
      id: "e1",
      stage_id: "load",
      generation: 1,
      state: "failed",
      worker_id: "worker",
      assignment_id: "assignment",
      job_id: null,
    },
    {
      id: "e2",
      stage_id: "load",
      generation: 2,
      state: "running",
      worker_id: "worker",
      assignment_id: "assignment2",
      job_id: null,
    },
    {
      id: "e3",
      stage_id: "check",
      generation: 1,
      state: "skipped",
      worker_id: null,
      assignment_id: null,
      job_id: null,
    },
  ],
  followups: [],
};
function fixtures(
  extra?: (
    path: string,
    options?: RequestInit,
  ) => Response | Promise<Response> | undefined,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, options?: RequestInit) => {
      const custom = extra?.(path, options);
      if (custom !== undefined) return custom;
      if (path.endsWith("/auth/session")) return json({ csrf: "test" });
      if (path === "/api/v1/projects")
        return json([
          { id: "a", name: "Alpha", slug: "alpha" },
          { id: "b", name: "Beta", slug: "beta" },
        ]);
      if (path.endsWith("/workflow-catalog"))
        return json({
          definition_schema: { type: "object" },
          actions: [
            {
              id: "core.echo@1",
              role: null,
              input_schema: { type: "object" },
              output_schema: { type: "object" },
            },
          ],
          executors: [
            { id: "builtin", available: true },
            { id: "docker", available: true },
            { id: "kubernetes", available: false },
            { id: "function", available: false },
          ],
        });
      if (path.endsWith("/workflow-definitions/def/revisions"))
        return json([
          {
            id: "r1",
            parent_id: null,
            definition_id: "def",
            created_at: 1,
            digest: "digest",
          },
        ]);
      if (path.endsWith("/workflow-definitions/def"))
        return json({ id: "def", name: "mixed-check", revision_id: "r1" });
      if (path.endsWith("/workflow-definitions"))
        return json([{ id: "def", name: "mixed-check", revision_id: "r1" }]);
      if (path.endsWith("/workflow-definition-revisions/r1"))
        return json({
          id: "r1",
          definition_id: "def",
          parent_id: null,
          digest: "digest",
          document: graph,
        });
      if (path.endsWith("/workflow-runs/run-1")) return json(receipt);
      if (path.endsWith("/workflow-runs")) return json([receipt]);
      if (path.endsWith("/repositories"))
        return json([{ id: "repo", alias: "backend", revision_id: "repo-r1" }]);
      if (path.endsWith("/worker-pools"))
        return json([
          {
            id: "pool",
            authorized: true,
            name: "shared",
            role: "simple",
            executors: ["builtin@1", "docker@1"],
            actions: ["core.echo@1", "docker.run@1"],
          },
        ]);
      if (path.includes("/usage"))
        return json({
          calls: 0,
          input_tokens: null,
          output_tokens: null,
          estimated_usd: null,
          reported_usd: null,
          unpriced_calls: 0,
          missing_usage_calls: 0,
          truncated: false,
        });
      if (path.includes("/events")) return json([]);
      return json({ id: "a", name: "Alpha" });
    }),
  );
}
function FixtureNavigation({ target }: { target: string }) {
  const navigate = useNavigate();
  return <button onClick={() => navigate(target)}>Go to fixture target</button>;
}
function mount(path = "/projects/a/workflows/def", target?: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        {target && <FixtureNavigation target={target} />}
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("displays server schema diagnostics and keeps a conflicting workflow draft with its original parent", async () => {
  const parents: string[] = [];
  fixtures((path, options) => {
    if (path.endsWith("/workflow-definitions/validate"))
      return json(
        {
          detail: {
            message: "Invalid workflow schema, graph or bindings",
            fields: [{ path: ["stages", 0, "inputs"], type: "value_error" }],
          },
        },
        422,
      );
    if (
      path.endsWith("/workflow-definitions/def") &&
      options?.method === "PUT"
    ) {
      parents.push(JSON.parse(String(options.body)).parent_id);
      return json({ detail: "Definition changed; reload before saving" }, 409);
    }
  });
  mount();
  const editor = await screen.findByLabelText("Workflow source");
  await waitFor(() =>
    expect((editor as HTMLTextAreaElement).value).toContain("mixed-check"),
  );
  fireEvent.change(editor, {
    target: { value: JSON.stringify({ ...graph, name: "edited-workflow" }) },
  });
  fireEvent.click(screen.getByRole("button", { name: "Validate definition" }));
  await screen.findByText(/stages.0.inputs/);
  fireEvent.click(screen.getByRole("button", { name: "Publish revision" }));
  await screen.findByText("Definition changed; reload before saving");
  expect((editor as HTMLTextAreaElement).value).toContain("edited-workflow");
  expect(parents).toEqual(["r1"]);
  expect(
    screen.getAllByText(/kubernetes.*unavailable/i).length,
  ).toBeGreaterThan(0);
});
it("submits only the saved revision and explicit bindings, treating acceptance as queued", async () => {
  let sent: unknown;
  fixtures((path, options) => {
    if (path.endsWith("/workflow-runs") && options?.method === "POST") {
      sent = JSON.parse(String(options.body));
      return json(receipt, 202);
    }
  });
  mount("/projects/a/workflow-revisions/r1");
  await screen.findByLabelText("Run input JSON");
  expect(screen.getByLabelText("Saved workflow document")).toHaveAttribute(
    "readonly",
  );
  await screen.findByRole("option", { name: "backend" });
  fireEvent.change(screen.getByLabelText("Default repository"), {
    target: { value: "backend" },
  });
  fireEvent.change(screen.getByLabelText("Run input JSON"), {
    target: { value: '{"message":"hello"}' },
  });
  fireEvent.change(screen.getByLabelText("Follow-up bindings JSON"), {
    target: { value: '{"repo":{"issue":"r1"}}' },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run saved revision" }));
  await screen.findByRole("heading", { name: "mixed-check" });
  expect(sent).toEqual({
    revision_id: "r1",
    input: { message: "hello" },
    bindings: { repo: { issue: "r1" } },
  });
  expect(await screen.findByText("pending")).toBeInTheDocument();
  expect(
    screen.getByText("Request accepted; execution is not complete."),
  ).toBeInTheDocument();
});
it("shows the pinned graph, retry attempts, skipped stages and unknown usage without inventing completion on cancel", async () => {
  let cancelled = false;
  fixtures((path, options) => {
    if (
      path.endsWith("/workflow-runs/run-1/cancel") &&
      options?.method === "POST"
    ) {
      cancelled = true;
      return json({ ...receipt, state: "cancelling" }, 202);
    }
    if (path.endsWith("/workflow-runs/run-1") && cancelled)
      return json({ ...receipt, state: "cancelling" });
  });
  mount("/projects/a/runs/run-1");
  await screen.findByRole("heading", { name: "mixed-check" });
  expect(await screen.findByText("Depends on: load")).toBeInTheDocument();
  expect(screen.getByText("skipped")).toBeInTheDocument();
  expect(screen.getByText("Attempt 2")).toBeInTheDocument();
  expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/pinned-model/).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "Request cancellation" }));
  await screen.findByText(
    "Cancellation requested; execution may still be running.",
  );
  expect(screen.queryByText("cancelled")).not.toBeInTheDocument();
  expect(
    screen.getAllByRole("link", { name: /^Worker worker$/ })[0],
  ).toHaveAttribute("href", "/projects/a/workers/worker");
});
it("rejects invalid YAML locally and displays server binding errors without pretending publication succeeded", async () => {
  let validations = 0;
  fixtures((path, options) => {
    if (path.endsWith("/workflow-definitions/validate")) {
      validations++;
      return json({ detail: "Repository not found" }, 404);
    }
    if (path.endsWith("/workflow-definitions/def") && options?.method === "PUT")
      return json({ detail: "Pool does not support stage requirements" }, 409);
  });
  mount();
  const editor = await screen.findByLabelText("Workflow source");
  await waitFor(() =>
    expect((editor as HTMLTextAreaElement).value).toContain("mixed-check"),
  );
  fireEvent.change(editor, { target: { value: "name: [" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate definition" }));
  await screen.findByRole("alert");
  expect(validations).toBe(0);
  fireEvent.change(editor, {
    target: {
      value: JSON.stringify({ ...graph, default_repository: "foreign" }),
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "Validate definition" }));
  await screen.findByText("Repository not found");
  expect(validations).toBe(1);
  expect((editor as HTMLTextAreaElement).value).toContain("foreign");
});
it("blocks foreign repository bindings and preserves input on missing follow-up binding errors", async () => {
  let requests = 0;
  fixtures((path, options) => {
    if (path.endsWith("/workflow-runs") && options?.method === "POST") {
      requests++;
      return json(
        {
          detail: "Every builtin repository requires pinned follow-up bindings",
        },
        422,
      );
    }
  });
  mount("/projects/a/workflow-revisions/r1");
  await screen.findByRole("option", { name: "backend" });
  fireEvent.change(screen.getByLabelText("Default repository"), {
    target: { value: "backend" },
  });
  fireEvent.change(screen.getByLabelText("Follow-up bindings JSON"), {
    target: { value: '{"foreign-repository":{"issue":"r1"}}' },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run saved revision" }));
  await screen.findByText(
    "Follow-up binding repository does not belong to this project.",
  );
  expect(requests).toBe(0);
  fireEvent.change(screen.getByLabelText("Follow-up bindings JSON"), {
    target: { value: "{}" },
  });
  fireEvent.change(screen.getByLabelText("Run input JSON"), {
    target: { value: '{"message":"keep me"}' },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run saved revision" }));
  await screen.findByText(
    "Every builtin repository requires pinned follow-up bindings",
  );
  expect(screen.getByLabelText("Run input JSON")).toHaveValue(
    '{"message":"keep me"}',
  );
  expect(requests).toBe(1);
});
it("keeps later project forms isolated from a delayed publish", async () => {
  let finish!: (r: Response) => void;
  fixtures((path, options) => {
    if (path.endsWith("/workflow-definitions/def") && options?.method === "PUT")
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
  });
  mount();
  const editor = await screen.findByLabelText("Workflow source");
  await waitFor(() =>
    expect((editor as HTMLTextAreaElement).value).toContain("mixed-check"),
  );
  fireEvent.change(editor, {
    target: { value: JSON.stringify({ ...graph, name: "alpha-draft" }) },
  });
  fireEvent.click(screen.getByRole("button", { name: "Publish revision" }));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  await waitFor(() =>
    expect(screen.getByLabelText("Project")).toHaveValue("b"),
  );
  await waitFor(() =>
    expect(
      (screen.getByLabelText("Workflow source") as HTMLTextAreaElement).value,
    ).toContain("mixed-check"),
  );
  fireEvent.change(screen.getByLabelText("Workflow source"), {
    target: { value: JSON.stringify({ ...graph, name: "beta-draft" }) },
  });
  await act(async () =>
    finish(json({ id: "def", name: "alpha-draft", revision_id: "r2" })),
  );
  expect(
    (screen.getByLabelText("Workflow source") as HTMLTextAreaElement).value,
  ).toContain("beta-draft");
  expect(
    screen.queryByText(/Published immutable revision/),
  ).not.toBeInTheDocument();
});
it("shows Docker cancellation intent, deadline and bounded logs separately from explicit model accounting", async () => {
  fixtures((path) => {
    if (path.endsWith("/jobs/job1"))
      return json({
        job_id: "job1",
        execution_id: "e3",
        state: "running",
        deadline: 1000,
        cancel_requested: true,
        error: null,
        result: {
          exit_code: null,
          stdout: "x".repeat(70000),
          stderr: "error detail",
          stdout_truncated: true,
          usage: [],
        },
      });
  });
  mount("/projects/a/jobs/job1?run=run-1");
  await screen.findByText(/Cancellation requested; terminal cleanup/);
  expect(screen.getByText(/Exit code: Unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Output truncated/)).toBeInTheDocument();
  expect(screen.getByText("error detail")).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "Return to project run" }),
  ).toHaveAttribute("href", "/projects/a/runs/run-1");
  expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
});
it.each(["worker", "cached job"])(
  "does not retain an exchange across a same-project %s change",
  async (kind) => {
    const event = {
      id: "event-a",
      kind: "model.completed",
      worker_id: "worker-a",
      occurred_at: 1,
      payload: { model: "test", conversation_available: true },
    };
    fixtures((path) => {
      if (path === "/api/v1/workers")
        return json({
          managed: [
            { id: "worker-a", name: "A", desired_state: "running" },
            { id: "worker-b", name: "B", desired_state: "running" },
          ],
          warnings: [],
          discovered: [],
          observed_at: 0,
        });
      if (path.endsWith("/jobs/job-a"))
        return json({
          job_id: "job-a",
          execution_id: "e-a",
          state: "completed",
          deadline: 1000,
          cancel_requested: false,
          result: null,
        });
      if (path.endsWith("/jobs/job-b"))
        return json({
          job_id: "job-b",
          execution_id: "e-b",
          state: "completed",
          deadline: 1000,
          cancel_requested: false,
          result: null,
        });
      if (path.includes("/events?"))
        return json(
          path.includes("worker-a") || path.includes("e-a") ? [event] : [],
        );
      if (path.endsWith("/model-calls/event-a/conversation"))
        return json({ exchange: "entity A transcript" });
    });
    const start =
      kind === "worker"
        ? "/projects/a/workers/worker-a"
        : "/projects/a/jobs/job-a";
    const target =
      kind === "worker"
        ? "/projects/a/workers/worker-b"
        : "/projects/a/jobs/job-b";
    const client = mount(start, target);
    await screen.findByRole("button", { name: "View exchange event-a" });
    fireEvent.click(
      screen.getByRole("button", { name: "View exchange event-a" }),
    );
    await screen.findByText(/entity A transcript/);
    if (kind === "cached job")
      client.setQueryData(["project", "a", "/jobs/job-b"], {
        job_id: "job-b",
        execution_id: "e-b",
        state: "completed",
        deadline: 1000,
        cancel_requested: false,
        result: null,
      });
    fireEvent.click(
      screen.getByRole("button", { name: "Go to fixture target" }),
    );
    await screen.findByRole("heading", {
      name: kind === "worker" ? "Worker B" : "Job job-b",
    });
    await waitFor(() =>
      expect(screen.queryByText(/entity A transcript/)).not.toBeInTheDocument(),
    );
  },
);
it.each(["terminal refresh", "later polling"])(
  "lets canonical terminal state supersede cancellation guidance after %s",
  async (mode) => {
    let requested = false;
    fixtures((path, options) => {
      if (
        path.endsWith("/workflow-runs/run-1/cancel") &&
        options?.method === "POST"
      ) {
        requested = true;
        return json({ ...receipt, state: "cancelling" }, 202);
      }
      if (path.endsWith("/workflow-runs/run-1") && requested)
        return json({
          ...receipt,
          state: mode === "terminal refresh" ? "cancelled" : "cancelling",
        });
    });
    const client = mount("/projects/a/runs/run-1");
    await screen.findByText("pending");
    fireEvent.click(
      screen.getByRole("button", { name: "Request cancellation" }),
    );
    if (mode === "later polling") {
      await screen.findByText(
        "Cancellation requested; execution may still be running.",
      );
      await act(async () => {
        client.setQueryData(["project", "a", "/workflow-runs/run-1"], {
          ...receipt,
          state: "cancelled",
        });
      });
    }
    await screen.findByText("cancelled");
    await waitFor(() =>
      expect(
        screen.queryByText(
          "Cancellation requested; execution may still be running.",
        ),
      ).not.toBeInTheDocument(),
    );
  },
);
it.each([
  [0.25, "$0.2500"],
  [0, "$0.0000"],
  [undefined, "Unknown"],
])(
  "displays the model event reported_cost_usd field %s without merging accounting sources",
  async (cost, want) => {
    fixtures((path) => {
      if (path.includes("/events?"))
        return json([
          {
            id: "cost-event",
            kind: "model.completed",
            worker_id: "worker",
            occurred_at: 1,
            payload: { model: "cost-model", reported_cost_usd: cost },
          },
        ]);
    });
    mount("/projects/a/runs/run-1");
    await screen.findByText("cost-model");
    const modelRow = screen.getByText("cost-model").closest("tr");
    expect(modelRow?.cells[4]).toHaveTextContent(want);
  },
);
it.each(["success", "revision failure", "delayed revision"])(
  "fetches the actual head before replacing a conflicted draft: %s",
  async (mode) => {
    let conflict = false;
    let finish!: (r: Response) => void;
    fixtures((path, options) => {
      if (
        path.endsWith("/workflow-definitions/def") &&
        options?.method === "PUT"
      ) {
        conflict = true;
        return json(
          { detail: "Definition changed; reload before saving" },
          409,
        );
      }
      if (path.endsWith("/workflow-definitions/def") && conflict)
        return json({ id: "def", name: "new-head", revision_id: "r2" });
      if (path.endsWith("/workflow-definition-revisions/r2")) {
        if (mode === "revision failure")
          return json({ detail: "Reload unavailable" }, 503);
        if (mode === "delayed revision")
          return new Promise<Response>((resolve) => {
            finish = resolve;
          });
        return json({
          id: "r2",
          definition_id: "def",
          document: { ...graph, name: "actual-latest" },
        });
      }
    });
    mount();
    const editor = await screen.findByLabelText("Workflow source");
    await waitFor(() =>
      expect((editor as HTMLTextAreaElement).value).toContain("mixed-check"),
    );
    fireEvent.change(editor, {
      target: {
        value: JSON.stringify({ ...graph, name: "keep-conflicted-draft" }),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Publish revision" }));
    await screen.findByText("Definition changed; reload before saving");
    fireEvent.click(
      screen.getByRole("button", { name: "Discard edits and load latest" }),
    );
    if (mode === "revision failure") {
      await screen.findByText("Reload unavailable");
      expect((editor as HTMLTextAreaElement).value).toContain(
        "keep-conflicted-draft",
      );
    } else {
      if (mode === "delayed revision") {
        await waitFor(() => expect(finish).toBeDefined());
        expect((editor as HTMLTextAreaElement).value).toContain(
          "keep-conflicted-draft",
        );
        expect(editor).toHaveAttribute("readonly");
        await act(async () =>
          finish(
            json({
              id: "r2",
              definition_id: "def",
              document: { ...graph, name: "actual-latest" },
            }),
          ),
        );
      }
      await waitFor(() =>
        expect((editor as HTMLTextAreaElement).value).toContain(
          "actual-latest",
        ),
      );
    }
  },
);
it("selects stage summaries without losing project-scoped attempt inspection", async () => {
  fixtures();
  mount("/projects/a/runs/run-1");
  const navigation = await screen.findByRole("navigation", {
    name: "Select workflow stage",
  });
  expect(navigation).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /2.*check/ }));
  expect(
    screen.queryByRole("link", { name: "Inspect attempt 1 of load" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "All stages" }));
  expect(
    screen.getByRole("link", { name: "Inspect attempt 1 of load" }),
  ).toHaveAttribute("href", "/projects/a/runs/run-1?stage=load&execution=e1");
});
