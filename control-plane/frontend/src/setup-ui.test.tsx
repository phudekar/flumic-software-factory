// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "./app/App";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const graph = {
  schema_version: 1,
  name: "custom-check",
  repositories: ["backend"],
  default_repository: "backend",
  stages: [
    {
      id: "check",
      executor: "builtin",
      action: "core.echo@1",
      role: "simple",
      pool_id: "pool",
      after: [],
    },
  ],
};
function mount(
  path: string,
  failGraph = false,
  credentialId: string | null = null,
) {
  const fetcher = vi.fn(async (path: string, _options?: RequestInit) => {
    if (path.endsWith("/auth/session")) return json({ csrf: "test" });
    if (path === "/api/v1/projects")
      return json([
        { id: "a", name: "Alpha", slug: "alpha" },
        { id: "b", name: "Beta", slug: "beta" },
      ]);
    if (path.endsWith("/repositories"))
      return json([
        {
          id: "repo",
          alias: "backend",
          owner: "acme",
          name: "api",
          provider: "github",
          revision_id: "repo-r1",
          overrides: {},
          credential_id: credentialId,
        },
      ]);
    if (path.endsWith("/secrets"))
      return json([
        {
          id: "secret-reference-uuid",
          name: "Source access",
          purpose: "github",
          repository_id: null,
          revoked_at: null,
        },
      ]);
    if (path.endsWith("/workflow-definitions"))
      return json([{ id: "def", name: "custom-check", revision_id: "r1" }]);
    if (path.endsWith("/workflow-definition-revisions/r1"))
      return failGraph
        ? json({ detail: "Revision unavailable" }, 503)
        : json({
            id: "r1",
            definition_id: "def",
            document: graph,
            parent_id: null,
            digest: "digest",
          });
    if (path.endsWith("/config/revisions") || path.endsWith("/worker-pools"))
      return json([]);
    if (path.includes("/workflow-runs?")) return json([]);
    if (path.endsWith("/events?kind=model.completed"))
      return json([
        {
          id: "call-" + (path.includes("/projects/b/") ? "b" : "a"),
          kind: "model.completed",
          stage: "plan",
          payload: {
            provider: "example",
            model: path.includes("/projects/b/") ? "beta-model" : "alpha-model",
            conversation_available: true,
          },
        },
      ]);
    if (path.includes("/model-calls/"))
      return json({ summary: "Scoped exchange" });
    return json({ id: "a", name: "Alpha" });
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, refetchInterval: false } },
        })
      }
    >
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetcher;
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("shows repositories before opening a connection form and cancels without saving", async () => {
  const fetcher = mount("/projects/a/repositories");
  await screen.findByText("acme/api");
  expect(screen.queryByLabelText("Repository alias")).not.toBeInTheDocument();
  expect(screen.getByText(/Missing credential/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  fireEvent.change(screen.getByLabelText("Repository alias"), {
    target: { value: "discard-me" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByLabelText("Repository alias")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  expect(screen.getByLabelText("Repository alias")).toHaveValue("");
  expect(fetcher.mock.calls.some(([, o]) => o?.method === "POST")).toBe(false);
});
it("keeps project creation out of the default list and clears cancelled drafts", async () => {
  mount("/projects");
  await screen.findByRole("link", { name: "Alpha" });
  expect(screen.queryByLabelText("Project name")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add project" }));
  expect(screen.getByLabelText("Project name")).toHaveFocus();
  fireEvent.change(screen.getByLabelText("Project name"), {
    target: { value: "Discard" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByLabelText("Project name")).not.toBeInTheDocument();
});
it("clears a cancelled credential value and never shows it in the list", async () => {
  const fetcher = mount("/projects/a/credentials");
  await screen.findByText("Source access");
  expect(screen.queryByLabelText("Secret value")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add credential" }));
  fireEvent.change(screen.getByLabelText("Secret value"), {
    target: { value: "private-draft" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Add credential" }));
  expect(screen.getByLabelText("Secret value")).toHaveValue("");
  expect(fetcher.mock.calls.some(([, o]) => o?.method === "POST")).toBe(false);
});
it("summarizes saved workflows and navigates to a revision without launching", async () => {
  const fetcher = mount("/projects/a/workflows");
  const card = await screen.findByRole("article", { name: "custom-check" });
  expect(await within(card).findByText("1 stage")).toBeInTheDocument();
  expect(within(card).getByText("backend")).toBeInTheDocument();
  fireEvent.click(within(card).getByRole("link", { name: "View" }));
  await screen.findByRole("heading", { name: "Stages" });
  expect(screen.getByRole("heading", { name: "check" })).toBeInTheDocument();
  expect(
    fetcher.mock.calls.some(
      ([p, o]) => p.endsWith("/workflow-runs") && o?.method === "POST",
    ),
  ).toBe(false);
});
it("shows unavailable workflow summaries as errors rather than empty graphs", async () => {
  mount("/projects/a/workflows", true);
  await screen.findByText("Revision unavailable");
  expect(screen.queryByText("0 stages")).not.toBeInTheDocument();
});

it("opens a saved launch form without submitting a run", async () => {
  const fetcher = mount("/projects/a/workflows");
  const card = await screen.findByRole("article", { name: "custom-check" });
  fireEvent.click(await within(card).findByRole("link", { name: "Run" }));
  await screen.findByRole("heading", { name: "Run saved revision" });
  expect(
    fetcher.mock.calls.some(
      ([p, o]) => p.endsWith("/workflow-runs") && o?.method === "POST",
    ),
  ).toBe(false);
});
it("edits a repository only after selecting it and cancels without a write", async () => {
  const fetcher = mount("/projects/a/repositories");
  fireEvent.click(await screen.findByRole("button", { name: "Edit backend" }));
  expect(screen.getByLabelText("Repository alias")).toHaveValue("backend");
  fireEvent.change(screen.getByLabelText("Repository alias"), {
    target: { value: "discard" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit backend" }));
  expect(screen.getByLabelText("Repository alias")).toHaveValue("backend");
  expect(fetcher.mock.calls.some(([, o]) => o?.method === "PUT")).toBe(false);
});

it("shows the assigned credential name without exposing a secret value", async () => {
  mount("/projects/a/repositories", false, "secret-reference-uuid");
  expect(
    await screen.findByText("Credential: Source access"),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("Secret value")).not.toBeInTheDocument();
});

it("shows only project navigation while a project workspace is selected", async () => {
  mount("/projects/a/repositories");
  const nav = await screen.findByRole("navigation", {
    name: "Project navigation",
  });
  for (const name of [
    "Overview",
    "Workflows",
    "Workflow runs",
    "Model history",
    "Usage & cost",
    "Repositories",
    "Credentials",
  ]) {
    expect(within(nav).getByRole("link", { name })).toHaveAttribute(
      "href",
      expect.stringContaining("/projects/a/"),
    );
  }
  expect(
    screen.queryByRole("link", { name: "Workers" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "Runtime targets" }),
  ).not.toBeInTheDocument();
});
it("queries project-only data in overview, usage and model history", async () => {
  const fetcher = mount("/projects/a/overview");
  await screen.findByRole("heading", { name: "Project overview" });
  const nav = screen.getByRole("navigation", { name: "Project navigation" });
  fireEvent.click(within(nav).getByRole("link", { name: "Usage & cost" }));
  await screen.findByRole("heading", { name: "Usage & cost" });
  fireEvent.click(within(nav).getByRole("link", { name: "Model history" }));
  await screen.findByRole("heading", { name: "Model history" });
  expect(
    fetcher.mock.calls.some(
      ([p]) =>
        p === "/api/v1/usage" ||
        p === "/api/v1/runs" ||
        p === "/api/v1/workers" ||
        p === "/api/v1/events?kind=model.completed",
    ),
  ).toBe(false);
  expect(
    fetcher.mock.calls.some(([p]) => p === "/api/v1/projects/a/usage"),
  ).toBe(true);
  expect(
    fetcher.mock.calls.some(
      ([p]) => p === "/api/v1/projects/a/events?kind=model.completed",
    ),
  ).toBe(true);
});

it("switches model history and exchange queries with the selected project", async () => {
  const fetcher = mount("/projects/a/history");
  await screen.findByText("example / alpha-model");
  fireEvent.click(screen.getByRole("button", { name: "View exchange call-a" }));
  await screen.findByText(/Scoped exchange/);
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  await screen.findByText("example / beta-model");
  expect(screen.queryByText("example / alpha-model")).not.toBeInTheDocument();
  expect(screen.queryByText(/Scoped exchange/)).not.toBeInTheDocument();
  expect(
    fetcher.mock.calls.some(
      ([p]) => p === "/api/v1/projects/a/model-calls/call-a/conversation",
    ),
  ).toBe(true);
  expect(
    fetcher.mock.calls.some(([p]) => p.startsWith("/api/v1/model-calls/")),
  ).toBe(false);
});
