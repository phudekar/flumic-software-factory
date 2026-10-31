// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
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
const docker = {
  id: "local-docker",
  name: "Local Docker",
  runtime: "docker",
  availability: "configured",
  capabilities: {
    create: true,
    legacy: true,
    shared: true,
    container_actions: true,
    log_stream: "split",
  },
};
const kube = {
  id: "k3d-test",
  name: "Local Kubernetes",
  runtime: "kubernetes",
  availability: "configured",
  capabilities: {
    create: true,
    legacy: false,
    shared: true,
    container_actions: true,
    log_stream: "combined",
  },
  limitations: ["Executable scratch", "Retained worker history volume"],
  auth: {
    token: "must-not-enter-dom-or-cache",
    kubeconfig: "/private/operator-config",
  },
};
function mount(
  path: string,
  targets: unknown[] = [docker, kube],
  handler?: (
    path: string,
    init?: RequestInit,
  ) => Promise<Response> | Response | undefined,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const custom = handler?.(input, init);
      if (custom) return await custom;
      if (input.endsWith("/auth/session")) return json({ csrf: "fixture" });
      if (input.endsWith("/targets")) return json(targets);
      if (input.endsWith("/projects"))
        return json([
          { id: "a", name: "Alpha", slug: "alpha" },
          { id: "b", name: "Beta", slug: "beta" },
        ]);
      if (input.endsWith("/worker-pools"))
        return json([
          { id: "simple-pool", name: "Shared simple", role: "simple" },
        ]);
      if (input.endsWith("/worker-profiles")) return json([]);
      if (input.endsWith("/workers"))
        return json({
          managed: [],
          discovered: [],
          warnings: [],
          observed_at: 0,
        });
      return json({ detail: "Not found" }, 404);
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
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

describe("runtime targets", () => {
  it("discovers registered targets and strips private fields before query caching", async () => {
    const client = mount("/runtime-targets");
    expect(
      await screen.findByRole("heading", { name: "Runtime targets" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Local Kubernetes")).toBeInTheDocument();
    expect(screen.getByText("Executable scratch")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(
      "must-not-enter-dom-or-cache",
    );
    const cached = JSON.stringify(
      client
        .getQueryCache()
        .getAll()
        .map((query) => query.state.data),
    );
    expect(cached).not.toContain("must-not-enter-dom-or-cache");
    expect(cached).not.toContain("/private/operator-config");
  });
  it("creates a shared worker with the selected pool and runtime target", async () => {
    mount("/workers", [docker, kube], (path, init) =>
      path.endsWith("/worker-pools/simple-pool/workers") &&
      init?.method === "POST"
        ? json({ id: "new-shared" }, 201)
        : undefined,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Add shared worker" }),
    );
    const target = await screen.findByLabelText("Deployment target");
    await screen.findByRole("option", { name: /Local Kubernetes/ });
    fireEvent.change(target, { target: { value: "k3d-test" } });
    fireEvent.change(screen.getByLabelText("Worker pool"), {
      target: { value: "simple-pool" },
    });
    fireEvent.change(screen.getByLabelText("Worker name"), {
      target: { value: "native-worker-2" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Create shared worker" }),
    );
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([path, init]) =>
              String(path).endsWith("/worker-pools/simple-pool/workers") &&
              init?.method === "POST",
          ),
      ).toBe(true),
    );
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([path, init]) =>
          String(path).endsWith("/worker-pools/simple-pool/workers") &&
          init?.method === "POST",
      )!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      name: "native-worker-2",
      target_id: "k3d-test",
    });
  });
  it("blocks unavailable shared targets without offering retired worker creation", async () => {
    mount("/workers", [
      docker,
      {
        ...kube,
        availability: "unavailable",
        capabilities: { ...kube.capabilities, create: false },
      },
    ]);
    fireEvent.click(
      await screen.findByRole("button", { name: "Add shared worker" }),
    );
    const target = await screen.findByLabelText("Deployment target");
    await screen.findByRole("option", { name: /Local Kubernetes/ });
    fireEvent.change(target, { target: { value: "k3d-test" } });
    expect(
      screen.getByRole("button", { name: "Create shared worker" }),
    ).toBeDisabled();
    expect(screen.getByText(/target is unavailable/i)).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Close shared worker form" }),
    );
    expect(
      screen.queryByRole("button", { name: "Create worker" }),
    ).not.toBeInTheDocument();
  });
});
it("shows Pending observation separately from desired state and registration", async () => {
  mount("/workers/worker-a", [docker, kube], (path) =>
    path.endsWith("/workers")
      ? json({
          managed: [
            {
              id: "worker-a",
              name: "Native pending",
              profile: "simple",
              target_id: "k3d-test",
              revision_id: "revision",
              desired_state: "running",
              operations: [],
              engine: null,
              runtime: {
                id: "kubernetes:k3d-test:dep-a",
                status: "pending",
                runtime_kind: "kubernetes",
                namespace: "flumic-workers",
                deployment_uid: "dep-a",
                pod_uids: ["pod-pending"],
                image: "registry.example/worker@sha256:abc",
              },
            },
          ],
          discovered: [],
          warnings: [],
        })
      : undefined,
  );
  expect(await screen.findByText("flumic-workers")).toBeInTheDocument();
  expect(screen.getByText("dep-a")).toBeInTheDocument();
  expect(screen.getByText("pod-pending")).toBeInTheDocument();
  expect(
    screen.getByText(/Hatchet registration: Not observed/),
  ).toBeInTheDocument();
  expect(screen.getByText(/Runtime observation: pending/)).toBeInTheDocument();
});
const job = {
  job_id: "job-a",
  execution_id: "execution-a",
  state: "completed",
  deadline: 2000000000,
  cancel_requested: false,
  error: null,
  target_id: "k3d-test",
  runtime: {
    kind: "kubernetes",
    namespace: "flumic-actions",
    job_uid: "job-uid-a",
    pod_uids: ["pod-uid-a"],
    phase: "captured",
    cleanup_pending: true,
    log_stream: "combined",
  },
  result: {
    exit_code: 0,
    stdout: "joined action logs",
    stderr: "",
    output: { ok: true },
    usage: [],
  },
};
describe("native Job metadata", () => {
  it("labels combined logs and shows scoped namespace, UID and cleanup state", async () => {
    mount("/projects/a/jobs/job-a", [docker, kube], (path) =>
      path.endsWith("/projects/a/jobs/job-a") ? json(job) : undefined,
    );
    expect(
      await screen.findByRole("heading", { name: "Combined action logs" }),
    ).toBeInTheDocument();
    expect(screen.getByText("flumic-actions")).toBeInTheDocument();
    expect(screen.getByText("job-uid-a")).toBeInTheDocument();
    expect(screen.getByText("pod-uid-a")).toBeInTheDocument();
    expect(screen.getByText(/cleanup pending/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "stderr" }),
    ).not.toBeInTheDocument();
  });
  it("retains split stdout and stderr for Docker jobs", async () => {
    mount("/projects/a/jobs/job-a", [docker], (path) =>
      path.endsWith("/projects/a/jobs/job-a")
        ? json({
            ...job,
            target_id: "local-docker",
            runtime: {
              kind: "docker",
              log_stream: "split",
              phase: "captured",
              cleanup_pending: false,
            },
          })
        : undefined,
    );
    expect(
      await screen.findByRole("heading", { name: "stdout" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "stderr" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Combined action logs" }),
    ).not.toBeInTheDocument();
  });
  it("does not apply late native metadata after switching projects", async () => {
    let resolve!: (value: Response) => void;
    mount("/projects/a/jobs/job-a", [docker, kube], (path) =>
      path.endsWith("/projects/a/jobs/job-a")
        ? new Promise<Response>((done) => {
            resolve = done;
          })
        : undefined,
    );
    await screen.findByRole("option", { name: "Beta" });
    await waitFor(() => expect(resolve).toBeDefined());
    fireEvent.change(screen.getByLabelText("Project"), {
      target: { value: "b" },
    });
    await screen.findByRole("alert");
    resolve(json(job));
    await waitFor(() =>
      expect(screen.queryByText("job-uid-a")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("flumic-actions")).not.toBeInTheDocument();
  });
});
