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
  await screen.findByRole("button", { name: "Edit alpha-repo" });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
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
  await screen.findByRole("button", { name: "Edit beta-repo" });
  expect(screen.queryByText("alpha-repo")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Repository alias")).not.toBeInTheDocument();
  await act(async () => finishSave(json({ id: "new", alias: "new-alpha" })));
  expect(screen.queryByText("Repository saved.")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Repository alias")).not.toBeInTheDocument();
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
  await screen.findByRole("button", { name: "Edit beta-repo" });
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
            repository_revision_id: "ar1",
            factory_revision_id: "f1",
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
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit alpha-repo" }),
  );
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
            revision_id: "ar1",
            overrides: {},
          },
          {
            id: "ar2",
            alias: "two",
            owner: "alpha",
            name: "two",
            revision_id: "ar2rev",
            overrides: {},
          },
        ]);
      return json([]);
    }),
  );
  mount();
  await screen.findByRole("button", { name: "Edit one" });
  fireEvent.click(
    screen.getByRole("button", { name: "Preview effective configuration" }),
  );
  await waitFor(() => expect(finish).toBeDefined());
  expect(screen.getByLabelText("Preview repository")).toBeDisabled();
  await act(async () =>
    finish(
      json({
        repository_id: "ar",
        repository_revision_id: "ar1",
        factory_revision_id: "f1",
        settings: { openai_model: "first-model" },
      }),
    ),
  );
  expect(screen.getByText(/first-model/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Preview repository"), {
    target: { value: "ar2" },
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
it("keeps dirty project defaults when head refreshes and submits the draft baseline", async () => {
  let head = "a1";
  const parents: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/config/revisions") && options?.method === "POST") {
        parents.push(JSON.parse(String(options.body)).parent_id);
        return new Response(
          JSON.stringify({
            detail: "Configuration changed; reload before saving",
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      if (input.endsWith("/config/revisions"))
        return json([{ id: head, summary: "Defaults" }]);
      if (input.endsWith("/config/revisions/a1"))
        return json({
          id: "a1",
          document: { defaults: { openai_model: "initial" } },
        });
      if (input.endsWith("/config/revisions/a2"))
        return json({
          id: "a2",
          document: { defaults: { openai_model: "external-change" } },
        });
      return json({});
    }),
  );
  const client = mount("/projects/a/config");
  await waitFor(() =>
    expect(
      (
        screen.getByLabelText(
          "Project configuration JSON",
        ) as HTMLTextAreaElement
      ).value,
    ).toContain("initial"),
  );
  fireEvent.change(screen.getByLabelText("Project configuration JSON"), {
    target: { value: '{"defaults":{"openai_model":"unsaved"}}' },
  });
  head = "a2";
  await act(async () => {
    await client.invalidateQueries({
      queryKey: ["project", "a", "/config/revisions"],
    });
  });
  await waitFor(() =>
    expect(
      client.getQueryData(["project", "a", "/config/revisions/a2"]),
    ).toBeDefined(),
  );
  expect(screen.getByLabelText("Project configuration JSON")).toHaveValue(
    '{"defaults":{"openai_model":"unsaved"}}',
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Save project configuration" }),
  );
  await screen.findByText("Configuration changed; reload before saving");
  expect(parents).toEqual(["a1"]);
  fireEvent.click(
    screen.getByRole("button", { name: "Discard edits and load latest" }),
  );
  expect(
    (screen.getByLabelText("Project configuration JSON") as HTMLTextAreaElement)
      .value,
  ).toContain("external-change");
});
it("does not let a delayed project creation replace a later selected project or its draft", async () => {
  let finish!: (r: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects" && options?.method === "POST")
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      if (input === "/api/v1/projects") return json(projects);
      if (
        input.endsWith("/repositories") ||
        input.endsWith("/secrets") ||
        input.endsWith("/config/revisions")
      )
        return json([]);
      return json({});
    }),
  );
  mount("/projects");
  fireEvent.click(await screen.findByRole("button", { name: "Add project" }));
  await screen.findByLabelText("Project name");
  fireEvent.change(screen.getByLabelText("Project name"), {
    target: { value: "Created" },
  });
  fireEvent.change(screen.getByLabelText("Project slug"), {
    target: { value: "created" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create project" }));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  fireEvent.click(await screen.findByRole("link", { name: "Repositories" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Add repository" }),
  );
  await screen.findByLabelText("Repository alias");
  fireEvent.change(screen.getByLabelText("Repository alias"), {
    target: { value: "beta-draft" },
  });
  await act(async () =>
    finish(json({ id: "created", name: "Created", slug: "created" })),
  );
  expect(screen.getByLabelText("Project")).toHaveValue("b");
  expect(screen.getByLabelText("Repository alias")).toHaveValue("beta-draft");
});
it("keeps preview repository identity stable when a new earlier alias is created", async () => {
  let added = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/config/effective"))
        return json({
          repository_id: "z",
          repository_revision_id: "z1",
          factory_revision_id: "f1",
          settings: { openai_model: "zulu-model" },
        });
      if (input.endsWith("/repositories") && options?.method === "POST") {
        added = true;
        return json({ id: "alpha" });
      }
      if (input.endsWith("/repositories"))
        return json([
          ...(added
            ? [
                {
                  id: "alpha",
                  alias: "alpha",
                  owner: "a",
                  name: "a",
                  revision_id: "a1",
                  overrides: {},
                },
              ]
            : []),
          {
            id: "z",
            alias: "zulu",
            owner: "z",
            name: "z",
            revision_id: "z1",
            overrides: {},
          },
        ]);
      return json([]);
    }),
  );
  mount();
  await screen.findByRole("button", { name: "Edit zulu" });
  fireEvent.click(
    screen.getByRole("button", { name: "Preview effective configuration" }),
  );
  await screen.findByText(/zulu-model/);
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  fireEvent.change(screen.getByLabelText("Repository alias"), {
    target: { value: "alpha" },
  });
  fireEvent.change(screen.getByLabelText("GitHub owner"), {
    target: { value: "a" },
  });
  fireEvent.change(screen.getByLabelText("Repository name"), {
    target: { value: "a" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save repository" }));
  await screen.findByRole("button", { name: "Edit alpha" });
  expect(screen.getByLabelText("Preview repository")).toHaveDisplayValue(
    "zulu",
  );
});
it("discards old effective results when a repository revision changes during preview", async () => {
  let revision = "z1";
  let head = "f1";
  let finish!: (r: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/config/revisions")) return json([{ id: head }]);
      if (input.endsWith("/config/effective"))
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      if (input.endsWith("/repositories"))
        return json([
          {
            id: "z",
            alias: "zulu",
            owner: "z",
            name: "z",
            revision_id: revision,
            overrides: {},
          },
        ]);
      return json([]);
    }),
  );
  const client = mount();
  await screen.findByRole("button", { name: "Edit zulu" });
  fireEvent.click(
    screen.getByRole("button", { name: "Preview effective configuration" }),
  );
  await waitFor(() => expect(finish).toBeDefined());
  revision = "z2";
  await act(async () => {
    await client.invalidateQueries({
      queryKey: ["project", "a", "/repositories"],
    });
  });
  await screen.findByText("Revision: z2");
  await act(async () =>
    finish(
      json({
        repository_id: "z",
        repository_revision_id: "z1",
        factory_revision_id: "f1",
        settings: { openai_model: "old-result" },
      }),
    ),
  );
  expect(screen.queryByText(/old-result/)).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: "Preview effective configuration" }),
  );
  await act(async () =>
    finish(
      json({
        repository_id: "z",
        repository_revision_id: "z2",
        factory_revision_id: "f1",
        settings: { openai_model: "current-result" },
      }),
    ),
  );
  expect(screen.getByText(/current-result/)).toBeInTheDocument();
  head = "f2";
  await act(async () => {
    await client.invalidateQueries({
      queryKey: ["project", "a", "/config/revisions"],
    });
  });
  await waitFor(() =>
    expect(screen.queryByText(/current-result/)).not.toBeInTheDocument(),
  );
});

it("configures a nested GitLab namespace with source-specific credentials and CI policy", async () => {
  let saved: Record<string, unknown> | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/secrets"))
        return json([
          {
            id: "gl-secret",
            name: "gitlab-source",
            purpose: "gitlab",
            repository_id: null,
            revoked_at: null,
          },
          {
            id: "gh-secret",
            name: "github-source",
            purpose: "github",
            repository_id: null,
            revoked_at: null,
          },
        ]);
      if (input.endsWith("/repositories") && options?.method === "POST") {
        saved = JSON.parse(String(options.body));
        return json({ id: "new-gitlab" });
      }
      return json([]);
    }),
  );
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Add repository" }),
  );
  fireEvent.change(await screen.findByLabelText("Source provider"), {
    target: { value: "gitlab" },
  });
  expect(
    screen.getByRole("option", { name: /gitlab-source/ }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: /github-source/ }),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Repository alias"), {
    target: { value: "backend" },
  });
  fireEvent.change(screen.getByLabelText("GitLab namespace"), {
    target: { value: "group/subgroup" },
  });
  fireEvent.change(screen.getByLabelText("Repository name"), {
    target: { value: "backend" },
  });
  fireEvent.change(screen.getByLabelText("GitLab credential reference"), {
    target: { value: "gl-secret" },
  });
  fireEvent.change(screen.getByLabelText("Required pipeline sources"), {
    target: { value: "push, merge_request_event" },
  });
  fireEvent.change(screen.getByLabelText("Required external statuses"), {
    target: { value: "security" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save repository" }));
  await waitFor(() =>
    expect(saved).toMatchObject({
      provider: "gitlab",
      host: "gitlab.com",
      owner: "group/subgroup",
      credential_id: "gl-secret",
      check_policy: {
        required_pipeline_sources: ["push", "merge_request_event"],
        required_status_names: ["security"],
      },
    }),
  );
});

it("validates a saved GitLab identity and displays its immutable project ID", async () => {
  const repo = {
    id: "gl",
    alias: "gitlab-backend",
    provider: "gitlab",
    host: "gitlab.com",
    owner: "group/subgroup",
    name: "backend",
    revision_id: "r1",
    overrides: {},
    validated: false,
    identity: null as null | { project_id: number },
    check_policy: {
      required_pipeline_sources: ["push"],
      required_status_names: [],
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (
        input.endsWith("/repositories/gl/validate") &&
        options?.method === "POST"
      ) {
        expect(JSON.parse(String(options.body))).toEqual({ parent_id: "r1" });
        repo.validated = true;
        repo.identity = { project_id: 123 };
        repo.revision_id = "r2";
        return json(repo);
      }
      if (input.endsWith("/repositories")) return json([repo]);
      return json([]);
    }),
  );
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit gitlab-backend" }),
  );
  expect(screen.getByLabelText("Source provider")).toBeDisabled();
  fireEvent.click(
    screen.getByRole("button", { name: "Validate GitLab identity" }),
  );
  expect(await screen.findByText(/GitLab project ID: 123/)).toBeInTheDocument();
});

it("keeps a stale GitLab validation conflict visible without inventing an identity", async () => {
  const repo = {
    id: "gl",
    alias: "gitlab-backend",
    provider: "gitlab",
    owner: "group/subgroup",
    name: "backend",
    revision_id: "r1",
    overrides: {},
    validated: false,
    identity: null,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input.endsWith("/auth/session")) return json({ csrf: "test" });
      if (input === "/api/v1/projects") return json(projects);
      if (input.endsWith("/repositories/gl/validate"))
        return new Response(
          JSON.stringify({
            detail: "Repository changed; reload before validation",
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      if (input.endsWith("/repositories")) return json([repo]);
      return json([]);
    }),
  );
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit gitlab-backend" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Validate GitLab identity" }),
  );
  expect(
    await screen.findByText("Repository changed; reload before validation"),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/GitLab identity is not validated/),
  ).toBeInTheDocument();
  expect(screen.queryByText(/GitLab project ID:/)).not.toBeInTheDocument();
});
