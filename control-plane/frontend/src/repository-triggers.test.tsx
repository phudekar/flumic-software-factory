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
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/a/triggers"]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}
const binding = {
  id: "binding-a",
  project_id: "a",
  repository_id: "gl",
  definition_id: "workflow",
  event_kind: "issue-ready",
  enabled: true,
  revision_id: "config-1",
  token_version_id: "token-1",
  configuration: {
    require_ready_label: true,
    input_defaults: {},
    followups: {},
  },
  webhook_url: "https://control.example/api/v1/hooks/gitlab/binding-a",
};
function fixture(
  write: (input: string, options: RequestInit) => Promise<Response>,
  rows: unknown[] = [],
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      if (options?.method === "POST" || options?.method === "PUT")
        return write(input, options);
      if (input.endsWith("/auth/session")) return json({ csrf: "csrf" });
      if (input === "/api/v1/projects")
        return json([
          { id: "a", name: "Alpha" },
          { id: "b", name: "Beta" },
        ]);
      if (input.endsWith("/repositories"))
        return json([
          {
            id: "gl",
            alias: "GitLab backend",
            provider: "gitlab",
            validated: true,
          },
          { id: "gh", alias: "GitHub backend", provider: "github" },
        ]);
      if (input.endsWith("/workflow-definitions"))
        return json([{ id: "workflow", name: "Issue workflow" }]);
      if (input.endsWith("/repository-triggers")) return json(rows);
      return json([]);
    }),
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
async function fill() {
  await screen.findByRole("option", { name: "GitLab backend" });
  fireEvent.change(await screen.findByLabelText("Trigger repository"), {
    target: { value: "gl" },
  });
  await screen.findByRole("option", { name: "Issue workflow" });
  fireEvent.change(screen.getByLabelText("Workflow definition"), {
    target: { value: "workflow" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save trigger" }));
}
it("shows newly created webhook tokens once and never caches their plaintext", async () => {
  fixture(async (input, options) => {
    expect(input).toBe("/api/v1/projects/a/repository-triggers");
    expect(JSON.parse(String(options.body))).toMatchObject({
      repository_id: "gl",
      definition_id: "workflow",
      event_kind: "issue-ready",
    });
    return json({ ...binding, webhook_token: "one-time-fixture-token" });
  });
  const cache = mount();
  await fill();
  expect(await screen.findByLabelText("Webhook token")).toHaveTextContent(
    "one-time-fixture-token",
  );
  expect(
    JSON.stringify(
      cache.getQueryData(["project", "a", "/repository-triggers"]),
    ),
  ).not.toContain("one-time-fixture-token");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss token" }));
  expect(screen.queryByText("one-time-fixture-token")).not.toBeInTheDocument();
});
it("discards a delayed token response when switching projects", async () => {
  let finish!: (value: Response) => void;
  fixture(
    async () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  mount();
  await fill();
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.change(screen.getByLabelText("Project"), {
    target: { value: "b" },
  });
  await act(async () =>
    finish(json({ ...binding, webhook_token: "late-alpha-token" })),
  );
  expect(screen.queryByText("late-alpha-token")).not.toBeInTheDocument();
});
it("rotates and revokes the current token version without caching token values", async () => {
  const calls: { input: string; body: unknown }[] = [];
  fixture(
    async (input, options) => {
      calls.push({ input, body: JSON.parse(String(options.body)) });
      if (input.endsWith("/rotate-token"))
        return json({
          ...binding,
          token_version_id: "token-2",
          webhook_token: "rotated-fixture-token",
        });
      return json({ ...binding, token_version_id: null });
    },
    [binding],
  );
  const cache = mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit binding-a" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Rotate webhook token" }));
  expect(await screen.findByLabelText("Webhook token")).toHaveTextContent(
    "rotated-fixture-token",
  );
  expect(calls[0]).toEqual({
    input: "/api/v1/projects/a/repository-triggers/binding-a/rotate-token",
    body: { parent_id: "token-1" },
  });
  expect(
    JSON.stringify(
      cache.getQueryData(["project", "a", "/repository-triggers"]),
    ),
  ).not.toContain("rotated-fixture-token");
  fireEvent.click(screen.getByRole("button", { name: "Revoke webhook token" }));
  await waitFor(() =>
    expect(calls[1]).toEqual({
      input: "/api/v1/projects/a/repository-triggers/binding-a/revoke-token",
      body: { parent_id: "token-2" },
    }),
  );
  expect(screen.queryByText("rotated-fixture-token")).not.toBeInTheDocument();
});
