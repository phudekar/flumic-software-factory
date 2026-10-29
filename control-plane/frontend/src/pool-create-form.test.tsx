// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ProjectProvider } from "./project-context";
import { PoolCreateForm } from "./features/pool-create-form";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
function mount(fail = false) {
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    if (options?.method === "POST")
      return fail
        ? json({ detail: "Pool name already exists" }, 409)
        : json({ id: "new-pool" }, 201);
    if (path.endsWith("/workflow-catalog"))
      return json({
        actions: [
          { id: "core.echo@1", role: null },
          { id: "issue.plan@1", role: "simple" },
          { id: "change.implement@1", role: "developer" },
        ],
      });
    return json({
      document: {
        profiles: {
          simple: { settings: { hatchet_tls_strategy: "none" } },
          developer: { settings: { hatchet_tls_strategy: "tls" } },
        },
      },
    });
  });
  vi.stubGlobal("fetch", fetcher);
  const onCreated = vi.fn(),
    onCancel = vi.fn();
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ProjectProvider id="project-a">
        <PoolCreateForm onCreated={onCreated} onCancel={onCancel} />
      </ProjectProvider>
    </QueryClientProvider>,
  );
  return { fetcher, onCreated, onCancel };
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("creates shared infrastructure separately from project authorization or worker startup", async () => {
  const { fetcher, onCreated } = mount();
  await screen.findByLabelText("issue.plan@1");
  await waitFor(() =>
    expect(screen.getByLabelText("Hatchet transport")).toHaveValue("none"),
  );
  fireEvent.change(screen.getByLabelText("Pool name"), {
    target: { value: "walkthrough-simple" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create pool" }));
  await waitFor(() =>
    expect(onCreated).toHaveBeenCalledWith("walkthrough-simple"),
  );
  const writes = fetcher.mock.calls.filter(
    ([, options]) => options?.method === "POST",
  );
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe("/api/v1/worker-pools");
  expect(JSON.parse(writes[0][1]!.body as string)).toEqual({
    name: "walkthrough-simple",
    role: "simple",
    actions: ["core.echo@1", "issue.plan@1"],
    executors: ["builtin@1"],
    normal_slots: 2,
    durable_slots: 16,
    hatchet_tls_strategy: "none",
  });
});
it("changes role capabilities and transport together and retains server errors in the form", async () => {
  const { fetcher, onCreated } = mount(true);
  await screen.findByLabelText("issue.plan@1");
  fireEvent.change(screen.getByLabelText("Worker role"), {
    target: { value: "developer" },
  });
  await screen.findByLabelText("change.implement@1");
  expect(screen.queryByLabelText("issue.plan@1")).not.toBeInTheDocument();
  expect(screen.getByLabelText("change.implement@1")).toBeChecked();
  expect(screen.getByLabelText("Hatchet transport")).toHaveValue("tls");
  fireEvent.change(screen.getByLabelText("Pool name"), {
    target: { value: "existing-pool" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create pool" }));
  await screen.findByText("Pool name already exists");
  expect(onCreated).not.toHaveBeenCalled();
  expect(screen.getByLabelText("Pool name")).toHaveValue("existing-pool");
  expect(
    JSON.parse(
      fetcher.mock.calls.find(([, o]) => o?.method === "POST")![1]!
        .body as string,
    ).role,
  ).toBe("developer");
});
it("does not create infrastructure when canceled or when no actions are selected", async () => {
  const { fetcher, onCancel } = mount();
  await screen.findByLabelText("issue.plan@1");
  fireEvent.click(screen.getByLabelText("core.echo@1"));
  fireEvent.click(screen.getByLabelText("issue.plan@1"));
  expect(screen.getByRole("button", { name: "Create pool" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onCancel).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls.some(([, o]) => o?.method === "POST")).toBe(false);
});
