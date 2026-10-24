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
import { MemoryRouter } from "react-router-dom";
import { App } from "./app/App";
const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
const projects = [
  { id: "a", slug: "alpha", name: "Alpha" },
  { id: "b", slug: "beta", name: "Beta" },
];
function mount(path = "/projects/a/repositories") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
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
it("does not reuse repositories or a delayed mutation result after switching projects", async () => {
  let finishSave!: (r: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/secrets")) return json([]);
      if (
        input === "/api/v1/projects/a/repositories" &&
        options?.method === "POST"
      )
        return new Promise<Response>((resolve) => {
          finishSave = resolve;
        });
      if (input === "/api/v1/projects/a/repositories")
        return json([
          {
            id: "ar",
            alias: "alpha-repo",
            owner: "alpha",
            name: "code",
            overrides: {},
            revision_id: "ar1",
          },
        ]);
      if (input === "/api/v1/projects/b/repositories")
        return json([
          {
            id: "br",
            alias: "beta-repo",
            owner: "beta",
            name: "code",
            overrides: {},
            revision_id: "br1",
          },
        ]);
      return json([]);
    }),
  );
  mount();
  await screen.findByRole("button", { name: "alpha-repo" });
  fireEvent.change(screen.getByLabelText("Repository alias"), {
    target: { value: "new-alpha" },
  });
  fireEvent.change(screen.getByLabelText("GitHub owner"), {
    target: { value: "alpha" },
  });
  fireEvent.change(screen.getByLabelText("Repository name"), {
    target: { value: "other" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save repository" }));
  await waitFor(() => expect(finishSave).toBeDefined());
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  await screen.findByRole("button", { name: "beta-repo" });
  expect(screen.queryByText("alpha-repo")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Repository alias")).toHaveValue("");
  await act(async () => finishSave(json({ id: "new", alias: "new-alpha" })));
  expect(screen.queryByText("Repository saved.")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Repository alias")).toHaveValue("");
});
it("ignores a delayed repository response after a project switch", async () => {
  let finishA!: (r: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input === "/api/v1/projects/a/repositories")
        return new Promise<Response>((resolve) => {
          finishA = resolve;
        });
      if (input === "/api/v1/projects/b/repositories")
        return json([
          {
            id: "br",
            alias: "beta-repo",
            owner: "beta",
            name: "code",
            overrides: {},
          },
        ]);
      return json([]);
    }),
  );
  const client = mount();
  await screen.findByLabelText("Project");
  await waitFor(() => expect(finishA).toBeDefined());
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  await screen.findByRole("button", { name: "beta-repo" });
  await act(async () => finishA(json([{ id: "ar", alias: "alpha-repo" }])));
  expect(screen.queryByText("alpha-repo")).not.toBeInTheDocument();
  expect(client.getQueryData(["project", "b", "/repositories"])).toEqual([
    {
      id: "br",
      alias: "beta-repo",
      owner: "beta",
      name: "code",
      overrides: {},
    },
  ]);
});
it("saves immutable repository edits with owned credential references and previews the selected alias", async () => {
  const requests: { path: string; body: unknown; method: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/secrets"))
        return json([
          {
            id: "github-a",
            name: "GitHub A",
            purpose: "github",
            repository_id: "ar",
            revoked_at: null,
          },
          {
            id: "openai-a",
            name: "OpenAI",
            purpose: "openai",
            revoked_at: null,
          },
          {
            id: "revoked",
            name: "Old token",
            purpose: "github",
            revoked_at: 42,
          },
        ]);
      if (options?.method !== "GET") {
        const body = JSON.parse(String(options?.body));
        requests.push({ path: input, body, method: String(options?.method) });
        if (input.endsWith("/config/effective"))
          return json({
            repository_id: "ar",
            settings: { openai_model: "custom-model" },
          });
        return json({ id: "ar" });
      }
      if (input.endsWith("/repositories"))
        return json([
          {
            id: "ar",
            alias: "alpha-repo",
            owner: "alpha",
            name: "code",
            overrides: {},
            revision_id: "ar1",
            credential_id: "github-a",
          },
        ]);
      return json({ id: "a", name: "Alpha" });
    }),
  );
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "alpha-repo" }));
  expect(screen.getByLabelText("GitHub credential reference")).toHaveValue(
    "github-a",
  );
  expect(
    screen.queryByRole("option", { name: /Old token/ }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: /OpenAI/ }),
  ).not.toBeInTheDocument();
  fireEvent.change(
    screen.getByLabelText("Repository behavior overrides (JSON)"),
    { target: { value: '{"openai_model":"custom-model"}' } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Save repository" }));
  await screen.findByText("Repository saved.");
  expect(requests[0]).toEqual({
    path: "/api/v1/projects/a/repositories/ar",
    method: "PUT",
    body: {
      alias: "alpha-repo",
      owner: "alpha",
      name: "code",
      credential_id: "github-a",
      overrides: { openai_model: "custom-model" },
      parent_id: "ar1",
    },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Preview effective configuration" }),
  );
  await screen.findByText(/"custom-model"/);
  expect(requests[1]).toEqual({
    path: "/api/v1/projects/a/config/effective",
    method: "POST",
    body: { repository: "alpha-repo", profile: "simple" },
  });
});
it("does not let a previous repository preview overwrite a changed selection", async () => {
  let finish!: (r: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/config/effective"))
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      if (input.endsWith("/repositories"))
        return json([
          {
            id: "ar",
            alias: "one",
            owner: "alpha",
            name: "one",
            overrides: {},
          },
          {
            id: "ar2",
            alias: "two",
            owner: "alpha",
            name: "two",
            overrides: {},
          },
        ]);
      return json([]);
    }),
  );
  mount();
  await screen.findByRole("button", { name: "one" });
  fireEvent.click(
    screen.getByRole("button", { name: "Preview effective configuration" }),
  );
  await waitFor(() => expect(finish).toBeDefined());
  expect(screen.getByLabelText("Preview repository")).toBeDisabled();
  await act(async () =>
    finish(json({ repository_id: "ar", settings: { model: "first-model" } })),
  );
  fireEvent.change(screen.getByLabelText("Preview repository"), {
    target: { value: "two" },
  });
  expect(screen.queryByText(/first-model/)).not.toBeInTheDocument();
});
it("shows pool eligibility by loaded action version and keeps membership writes scoped", async () => {
  const writes: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (options?.method === "PUT") {
        writes.push(input);
        return json({ enabled: true });
      }
      if (input.endsWith("/worker-pools"))
        return json([
          {
            id: "pool",
            name: "shared",
            role: "simple",
            normal_slots: 2,
            durable_slots: 16,
            actions: ["core.echo@1", "issue.load@1"],
            executors: ["builtin@1"],
            authorized: true,
            workers: [
              {
                id: "w",
                name: "worker-one",
                protocol: "shared-v1",
                desired_state: "running",
                capability_state: "partial",
                capabilities: {
                  actions: ["core.echo@1"],
                  executors: ["builtin@1"],
                },
                missing_actions: ["issue.load@1"],
                missing_executors: [],
                last_heartbeat_at: null,
              },
            ],
          },
        ]);
      return json({ id: "a", name: "Alpha" });
    }),
  );
  mount("/projects/a/pools");
  await screen.findByText("worker-one");
  expect(
    screen.getByText("Eligible registered slots: 2 normal / 16 durable"),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Missing action versions: issue.load@1"),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Required action in shared"), {
    target: { value: "issue.load@1" },
  });
  expect(
    screen.getByText("Eligible registered slots: 0 normal / 0 durable"),
  ).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: "Remove project authorization" }),
  );
  await waitFor(() =>
    expect(writes).toEqual(["/api/v1/projects/a/worker-pools/pool"]),
  );
});
it("clears project defaults forms and ignores an earlier project save result", async () => {
  let finish!: (r: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/config/revisions") && options?.method === "POST")
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      if (input === "/api/v1/projects/a/config/revisions")
        return json([{ id: "a1", summary: "Alpha defaults" }]);
      if (input === "/api/v1/projects/b/config/revisions")
        return json([{ id: "b1", summary: "Beta defaults" }]);
      if (input.endsWith("/config/revisions/a1"))
        return json({
          id: "a1",
          document: { defaults: { openai_model: "alpha-model" } },
        });
      if (input.endsWith("/config/revisions/b1"))
        return json({
          id: "b1",
          document: { defaults: { openai_model: "beta-model" } },
        });
      return json({});
    }),
  );
  mount("/projects/a/config");
  await waitFor(() =>
    expect(
      (
        screen.getByLabelText(
          "Project configuration JSON",
        ) as HTMLTextAreaElement
      ).value,
    ).toContain("alpha-model"),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Save project configuration" }),
  );
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  await waitFor(() =>
    expect(
      (
        screen.getByLabelText(
          "Project configuration JSON",
        ) as HTMLTextAreaElement
      ).value,
    ).toContain("beta-model"),
  );
  await act(async () => finish(json({ id: "a2" })));
  expect(
    screen.queryByText("Project configuration saved."),
  ).not.toBeInTheDocument();
  expect(
    (screen.getByLabelText("Project configuration JSON") as HTMLTextAreaElement)
      .value,
  ).toContain("beta-model");
});
