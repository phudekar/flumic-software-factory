// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ProjectProvider } from "../project-context";
import type { ModelAccountingEvent } from "../workflow-api";
import { ProjectModelCalls } from "./project-model-calls";
const event: ModelAccountingEvent = {
  id: "call-1",
  run_id: "run",
  attempt_id: null,
  kind: "model.completed",
  worker_id: "worker",
  occurred_at: 1793200000,
  stage: "plan",
  payload: {
    provider: "OpenAI",
    model: "planner",
    conversation_available: true,
    input_tokens: 0,
    outcome: "failed",
  },
};
function mount(events = [event], project = "a") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const tree = (id: string) => (
    <QueryClientProvider client={client}>
      <ProjectProvider id={id}>
        <ProjectModelCalls events={events} />
      </ProjectProvider>
    </QueryClientProvider>
  );
  return { ...render(tree(project)), tree };
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("filters metadata without fetching content and keeps parent exclusion explicit", () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  mount([
    event,
    {
      ...event,
      id: "parent",
      stage: "review",
      payload: { model: "reviewer", accounting_leaf: false },
    },
  ]);
  expect(screen.getByText(/Parent summary.*excluded/)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "View exchange parent" }),
  ).toBeDisabled();
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "planner" },
  });
  expect(
    screen.queryByRole("button", { name: "View exchange parent" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "failed" })).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});
it("loads scoped exchanges, renders nested JSON and messages safely, restores focus on Escape", async () => {
  const fetcher = vi.fn(
    async (_url: unknown, _init: unknown) =>
      new Response(
        JSON.stringify({
          coverage: "api_exchange",
          request_at: 1793200000,
          request: {
            messages: [
              { role: "system", content: "System context" },
              { role: "user", content: "<script>alert(1)</script>" },
              {
                role: "user",
                content: JSON.stringify({
                  issue_title: "Greeting task",
                  current_source_files: [
                    { path: "hello.py", content: "Source context text" },
                  ],
                }),
              },
            ],
          },
          response: JSON.stringify({
            tasks: [{ title: "Make greeting", steps: ["Print hello"] }],
          }),
          extra: "keep raw",
        }),
      ),
  );
  vi.stubGlobal("fetch", fetcher);
  mount();
  const opener = screen.getByRole("button", { name: "View exchange call-1" });
  opener.focus();
  fireEvent.click(opener);
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByText("Make greeting");
  expect(
    within(dialog).getByText("<script>alert(1)</script>"),
  ).toBeInTheDocument();
  expect(
    within(dialog).queryByText("Source context text"),
  ).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByText("Expand captured context"));
  await within(dialog).findByText("Source context text");
  expect(dialog.querySelector("script")).toBeNull();
  fireEvent.click(within(dialog).getByText("Complete original raw payload"));
  await within(dialog).findByText(/keep raw/);
  expect(fetcher.mock.calls[0]?.[0]).toBe(
    "/api/v1/projects/a/model-calls/call-1/conversation",
  );
  expect(fetcher.mock.calls[0]?.[1]).toMatchObject({
    signal: expect.any(AbortSignal),
  });
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
});
it("reports expired capture errors and dismisses with close and backdrop", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "Exchange expired" }), {
          status: 410,
        }),
    ),
  );
  mount();
  fireEvent.click(screen.getByRole("button", { name: "View exchange call-1" }));
  await screen.findByText(/Exchange expired/);
  fireEvent.click(screen.getByRole("button", { name: "Close exchange" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "View exchange call-1" }));
  fireEvent.click(screen.getByRole("dialog").parentElement!);
  expect(screen.queryByRole("dialog")).toBeNull();
});
it("fences a pending exchange when the selected project changes", async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn((_url, init) => {
      signal = init.signal;
      return new Promise(() => {});
    }),
  );
  const view = mount();
  fireEvent.click(screen.getByRole("button", { name: "View exchange call-1" }));
  expect(await screen.findByText("Loading exchange…")).toBeInTheDocument();
  view.rerender(view.tree("b"));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(signal?.aborted).toBe(true);
});
