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
import {
  focusManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "./app/App";
import { workflowPresentation } from "./components/workflow-summary";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const issue = {
  schema_version: 1,
  name: "factory-issue-backend-hash",
  repositories: ["backend"],
  default_repository: "backend",
  stages: [
    "issue.load@1",
    "issue.plan@1",
    "issue.triage@1",
    "change.implement@1",
    "change.review@1",
    "workflow.record@1",
  ].map((action, index) => ({
    id: "stage-" + index,
    executor: "builtin",
    action,
    role: action === "change.implement@1" ? "developer" : "simple",
    pool_id: "pool",
  })),
};
const simple = [
  "issue.load@1",
  "issue.plan@1",
  "issue.triage@1",
  "change.review@1",
  "workflow.record@1",
  "pull.verify@1",
];
function mount(
  path: string,
  failure?: "handover" | "seed" | "run" | "foreign",
) {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      if (url.endsWith("/seed-builtins"))
        return failure === "seed"
          ? json({ detail: "Pool is not authorized" }, 403)
          : json({
              issue: "issue-r1",
              feedback: "feedback-r1",
              verify: "verify-r1",
            });
      if (url.endsWith("/dynamic-mode"))
        return failure === "handover"
          ? json(
              {
                detail:
                  "Drain and stop legacy workers before enabling dynamic mode",
              },
              409,
            )
          : json({ mode: "dynamic", repository_id: "connection-uuid" });
      return json({ detail: "Dispatch unavailable" }, 503);
    }
    if (url.endsWith("/auth/session")) return json({ csrf: "test" });
    if (url === "/api/v1/projects")
      return json([{ id: "a", name: "Alpha", slug: "alpha" }]);
    if (url.endsWith("/repositories"))
      return json([
        {
          id: "connection-uuid",
          alias: "backend",
          owner: "example",
          name: "repo",
          revision_id: "repo-r1",
          credential_id: "github-secret",
          overrides: {},
        },
      ]);
    if (url.endsWith("/secrets"))
      return json([
        {
          id: "openai-ref",
          name: "Planner key",
          purpose: "openai",
          repository_id: "connection-uuid",
          revoked_at: null,
        },
        {
          id: "foreign-ref",
          name: "Other repo key",
          purpose: "openai",
          repository_id: "foreign",
          revoked_at: null,
        },
        {
          id: "revoked-ref",
          name: "Revoked key",
          purpose: "openai",
          repository_id: null,
          revoked_at: 1,
        },
      ]);
    if (url.endsWith("/worker-pools"))
      return json([
        {
          id: "simple-pool",
          name: "Simple pool",
          role: "simple",
          authorized: true,
          executors: ["builtin@1"],
          actions: simple,
        },
        {
          id: "dev-pool",
          name: "Developer pool",
          role: "developer",
          authorized: true,
          executors: ["builtin@1"],
          actions: ["change.implement@1", "pull.feedback@1"],
        },
        {
          id: "unauthorized",
          name: "Unapproved pool",
          role: "simple",
          authorized: false,
          executors: ["builtin@1"],
          actions: simple,
        },
      ]);
    if (url.endsWith("/workflow-definitions"))
      return json(
        ["issue", "feedback", "verify"].map((kind) => ({
          id: kind + "-def",
          name: "factory-" + kind + "-backend-hash",
          revision_id: kind + "-r1",
        })),
      );
    if (url.includes("/workflow-definition-revisions/")) {
      const id = url.split("/").at(-1)!;
      const graph =
        id === "issue-r1"
          ? issue
          : {
              ...issue,
              name: "factory-" + id,
              default_repository:
                failure === "foreign" && id === "feedback-r1"
                  ? "other-repo"
                  : "backend",
              stages: [
                {
                  id: "followup",
                  action:
                    id === "feedback-r1" ? "pull.feedback@1" : "pull.verify@1",
                  executor: "builtin",
                  role: "simple",
                  pool_id: "simple-pool",
                },
              ],
            };
      return json({
        id,
        definition_id: id + "-def",
        document: graph,
        digest: "digest",
      });
    }
    return json([]);
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
const writes = (f: ReturnType<typeof mount>) =>
  f.mock.calls.filter(([, o]) => o?.method === "POST");
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  focusManager.setFocused(undefined);
});
it("classifies standard workflows independently of their display titles", () => {
  expect(workflowPresentation(issue).kind).toBe("issue");
  expect(
    workflowPresentation({
      ...issue,
      name: "factory-feedback-backend",
      stages: [{ ...issue.stages[0], action: "pull.feedback@1" }],
    }).kind,
  ).toBe("feedback");
  expect(
    workflowPresentation({
      ...issue,
      name: "factory-verify-backend",
      stages: [{ ...issue.stages[0], action: "pull.verify@1" }],
    }).kind,
  ).toBe("verify");
  expect(
    workflowPresentation({ ...issue, stages: [issue.stages[0]] }).kind,
  ).toBe("custom");
});
async function chooseTemplates() {
  fireEvent.click(
    await screen.findByRole("button", { name: "Install templates" }),
  );
  fireEvent.change(await screen.findByLabelText("Target repository"), {
    target: { value: "backend" },
  });
  await screen.findByRole("option", { name: "Simple pool" });
  fireEvent.change(screen.getByLabelText("Simple worker pool"), {
    target: { value: "simple-pool" },
  });
  fireEvent.change(screen.getByLabelText("Developer worker pool"), {
    target: { value: "dev-pool" },
  });
  fireEvent.change(screen.getByLabelText("OpenAI credential grant"), {
    target: { value: "openai-ref" },
  });
}
it("installs templates with the alias and scoped credential IDs without launching", async () => {
  const f = mount("/projects/a/workflows");
  await chooseTemplates();
  expect(
    screen.queryByRole("option", { name: "Other repo key" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: "Revoked key" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: "Unapproved pool" }),
  ).not.toBeInTheDocument();
  fireEvent.click(
    screen
      .getAllByRole("button", { name: "Install templates" })
      .find((b) => !b.hasAttribute("disabled"))!,
  );
  await screen.findByRole("link", { name: "issue: view saved revision" });
  expect(writes(f)).toHaveLength(1);
  expect(writes(f)[0][0]).toBe(
    "/api/v1/projects/a/workflow-definitions/seed-builtins",
  );
  expect(JSON.parse(writes(f)[0][1]!.body as string)).toEqual({
    repository: "backend",
    simple_pool: "simple-pool",
    developer_pool: "dev-pool",
    secrets: { openai: "openai-ref" },
  });
  expect(
    screen.getByText(/Existing recipes were retained/),
  ).toBeInTheDocument();
});
it("retains installation errors and cancels without a further write", async () => {
  const f = mount("/projects/a/workflows", "seed");
  await chooseTemplates();
  fireEvent.click(
    screen
      .getAllByRole("button", { name: "Install templates" })
      .find((b) => !b.hasAttribute("disabled"))!,
  );
  await screen.findByText("Pool is not authorized");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(writes(f)).toHaveLength(1);
});
it("requires handover acknowledgment, displays a server conflict and never bypasses it", async () => {
  const f = mount("/projects/a/repositories", "handover");
  fireEvent.click(
    await screen.findByRole("button", {
      name: "Review project execution for backend",
    }),
  );
  expect(
    screen.getByRole("button", { name: "Enable project execution" }),
  ).toBeDisabled();
  expect(writes(f)).toHaveLength(0);
  expect(screen.getByRole("checkbox")).toHaveAccessibleName(
    /drained and stopped legacy workers.*verified.*none/i,
  );
  fireEvent.click(
    screen.getByLabelText(/I have reviewed repository ownership/),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Enable project execution" }),
  );
  await screen.findByText(
    "Drain and stop legacy workers before enabling dynamic mode",
  );
  expect(writes(f)[0][0]).toBe(
    "/api/v1/projects/a/repositories/connection-uuid/dynamic-mode",
  );
  expect(JSON.parse(writes(f)[0][1]!.body as string)).toEqual({});
  expect(
    screen.queryByText(/Project execution enabled/),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(writes(f)).toHaveLength(1);
});
it("reports a successful handover only after an explicit submission", async () => {
  const f = mount("/projects/a/repositories");
  fireEvent.click(
    await screen.findByRole("button", {
      name: "Review project execution for backend",
    }),
  );
  fireEvent.click(
    screen.getByLabelText(/I have reviewed repository ownership/),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Enable project execution" }),
  );
  await screen.findByText("Project execution enabled for backend.");
  expect(writes(f)).toHaveLength(1);
});
async function chooseRun() {
  fireEvent.change(await screen.findByLabelText("Default repository"), {
    target: { value: "backend" },
  });
  fireEvent.change(screen.getByLabelText("Issue number"), {
    target: { value: "42" },
  });
  await screen.findByRole("option", { name: /Feedback: backend/ });
  fireEvent.change(screen.getByLabelText("Feedback workflow revision"), {
    target: { value: "feedback-r1" },
  });
  fireEvent.change(screen.getByLabelText("Verification workflow revision"), {
    target: { value: "verify-r1" },
  });
}
it("builds UUID-based bindings and reuses the idempotency key for a failed run retry", async () => {
  const f = mount("/projects/a/workflow-revisions/issue-r1");
  await screen.findByLabelText("Issue number");
  expect(
    screen.getByRole("button", { name: "Run saved revision" }),
  ).toBeDisabled();
  await chooseRun();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Run saved revision" }),
    ).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Run saved revision" }));
  await screen.findByText("Dispatch unavailable");
  expect(JSON.parse(writes(f)[0][1]!.body as string)).toEqual({
    revision_id: "issue-r1",
    input: { issue_number: 42 },
    bindings: {
      "connection-uuid": {
        issue: "issue-r1",
        feedback: "feedback-r1",
        verify: "verify-r1",
      },
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run saved revision" }));
  await waitFor(() => expect(writes(f)).toHaveLength(2));
  expect(
    (writes(f)[0][1]!.headers as Record<string, string>)["Idempotency-Key"],
  ).toBe(
    (writes(f)[1][1]!.headers as Record<string, string>)["Idempotency-Key"],
  );
});
it("rejects a follow-up revision targeting a different repository", async () => {
  const f = mount("/projects/a/workflow-revisions/issue-r1", "foreign");
  await chooseRun();
  await screen.findByText(
    "Select compatible feedback and verification revisions for this repository.",
  );
  expect(
    screen.getByRole("button", { name: "Run saved revision" }),
  ).toBeDisabled();
  expect(writes(f)).toHaveLength(0);
});
it("preserves custom advanced JSON across mode changes", async () => {
  mount("/projects/a/workflow-revisions/issue-r1");
  await chooseRun();
  fireEvent.change(screen.getByLabelText("Run setup"), {
    target: { value: "advanced" },
  });
  fireEvent.change(screen.getByLabelText("Run input JSON"), {
    target: { value: '{"issue_number":99,"custom":true}' },
  });
  fireEvent.change(screen.getByLabelText("Run setup"), {
    target: { value: "guided" },
  });
  fireEvent.change(screen.getByLabelText("Run setup"), {
    target: { value: "advanced" },
  });
  expect(screen.getByLabelText("Run input JSON")).toHaveValue(
    '{"issue_number":99,"custom":true}',
  );
});

it("requires a new explicit follow-up selection after the listed revision changes", async () => {
  const fetcher = mount("/projects/a/workflow-revisions/issue-r1");
  await chooseRun();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Run saved revision" }),
    ).toBeEnabled(),
  );
  const previous = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (url, options) =>
    url.endsWith("/workflow-definitions") && options?.method !== "POST"
      ? json(
          ["issue", "feedback", "verify"].map((kind) => ({
            id: kind + "-def",
            name: "factory-" + kind + "-backend-hash",
            revision_id: kind + (kind === "feedback" ? "-r2" : "-r1"),
          })),
        )
      : previous(url, options),
  );
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Run saved revision" }),
    ).toBeDisabled(),
  );
  expect(writes(fetcher)).toHaveLength(0);
});
