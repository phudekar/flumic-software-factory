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
import { ProjectProvider } from "../project-context";
import { ExecutionModeForm, ExecutionModeChoice } from "./execution-mode-form";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";

const initial = {
  revision_id: "direct-v1",
  default_mode: "direct",
  minimum_mode: "direct",
  admitted_direct_runs: 2,
  protected_execution_available: false,
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
function mount({
  conflict = false,
  failed = false,
}: { conflict?: boolean; failed?: boolean } = {}) {
  let current = { ...initial };
  const fetcher = vi.fn(async (_path: string, options?: RequestInit) => {
    if (failed) return json({ detail: "Policy unavailable" }, 503);
    if (options?.method === "PUT") {
      if (conflict) {
        current = {
          ...current,
          revision_id: "newer",
          default_mode: "protected",
          minimum_mode: "protected",
        };
        return json(
          { detail: "Execution policy changed; reload before saving" },
          409,
        );
      }
      const body = JSON.parse(String(options.body));
      current = {
        ...current,
        revision_id: "saved",
        default_mode: body.default_mode,
        minimum_mode: body.minimum_mode,
      };
    }
    return json(current);
  });
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ProjectProvider id="project-a">
        <MemoryRouter>
          <ExecutionModeForm />
        </MemoryRouter>
      </ProjectProvider>
    </QueryClientProvider>,
  );
  return { fetcher, client };
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("explains both choices and requires explicit consent before saving a blocking protected default", async () => {
  const { fetcher } = mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit execution policy" }),
  );
  fireEvent.change(screen.getByLabelText("Default execution mode"), {
    target: { value: "protected" },
  });
  expect(screen.getByText(/protected execution is not ready/i)).toBeVisible();
  expect(
    screen.getByLabelText(
      /launches using this protected default.*explicit direct overrides remain allowed/i,
    ),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Save execution policy" }),
  ).toBeDisabled();
  expect(
    fetcher.mock.calls.filter(([, options]) => options?.method === "PUT"),
  ).toHaveLength(0);
  fireEvent.click(screen.getByLabelText(/understand.*block/i));
  fireEvent.click(
    screen.getByRole("button", { name: "Save execution policy" }),
  );
  await screen.findByText(/execution policy saved/i);
  const call = fetcher.mock.calls.find(
    ([, options]) => options?.method === "PUT",
  )!;
  expect(call[0]).toBe("/api/v1/projects/project-a/execution-policy");
  expect(JSON.parse(String(call[1]!.body))).toEqual({
    expected_revision: "direct-v1",
    default_mode: "protected",
    minimum_mode: "direct",
  });
});

it("raises the draft default when the minimum becomes protected and previews existing direct runs", async () => {
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit execution policy" }),
  );
  expect(screen.getByText(/2.*admitted direct runs/i)).toBeVisible();
  fireEvent.change(screen.getByLabelText("Minimum execution mode"), {
    target: { value: "protected" },
  });
  expect(screen.getByLabelText("Default execution mode")).toHaveValue(
    "protected",
  );
  const select = screen.getByLabelText(
    "Default execution mode",
  ) as HTMLSelectElement;
  expect(select.querySelector('option[value="direct"]')).toBeDisabled();
  expect(
    screen.getByLabelText(/requiring protected execution blocks all new runs/i),
  ).toBeVisible();
  expect(screen.getByText(/does not cancel/i)).toBeVisible();
});

it("retains unsaved edits on conflict and requires an explicit reload before another save", async () => {
  mount({ conflict: true });
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit execution policy" }),
  );
  fireEvent.change(screen.getByLabelText("Default execution mode"), {
    target: { value: "protected" },
  });
  fireEvent.click(screen.getByLabelText(/understand.*block/i));
  fireEvent.click(
    screen.getByRole("button", { name: "Save execution policy" }),
  );
  await screen.findByText(/your edits are retained/i);
  expect(screen.getByLabelText("Default execution mode")).toHaveValue(
    "protected",
  );
  expect(screen.getByLabelText("Minimum execution mode")).toHaveValue("direct");
  expect(
    screen.getByRole("button", { name: "Save execution policy" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: /cancel.*reload/i }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit execution policy" }),
  );
  expect(screen.getByLabelText("Minimum execution mode")).toHaveValue(
    "protected",
  );
});

it("does not offer policy editing when the server policy cannot be loaded", async () => {
  mount({ failed: true });
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Policy unavailable",
  );
  expect(
    screen.queryByRole("button", { name: "Edit execution policy" }),
  ).not.toBeInTheDocument();
});

it("keeps protected launch selection unavailable and explains a protected project minimum", () => {
  function Choice() {
    const [value, setValue] = useState<"project" | "direct" | "protected">(
      "project",
    );
    return (
      <ExecutionModeChoice
        policy={{
          ...initial,
          default_mode: "protected",
          minimum_mode: "protected",
        }}
        value={value}
        onChange={setValue}
      />
    );
  }
  render(<Choice />);
  const select = screen.getByLabelText(
    "Run execution mode",
  ) as HTMLSelectElement;
  expect(select.querySelector('option[value="direct"]')).toBeDisabled();
  expect(select.querySelector('option[value="protected"]')).toBeDisabled();
  expect(screen.getByText(/protected execution is not ready/i)).toBeVisible();
});

it("does not apply a completed policy write to a newly selected project", async () => {
  let resolve!: (response: Response) => void;
  const fetcher = vi.fn((path: string, options?: RequestInit) => {
    if (options?.method === "PUT")
      return new Promise<Response>((done) => {
        resolve = done;
      });
    return Promise.resolve(
      json({
        ...initial,
        revision_id: path.includes("project-b") ? "b-policy" : "direct-v1",
      }),
    );
  });
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = (id: string) => (
    <QueryClientProvider client={client}>
      <ProjectProvider id={id}>
        <MemoryRouter>
          <ExecutionModeForm />
        </MemoryRouter>
      </ProjectProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(view("project-a"));
  fireEvent.click(
    await screen.findByRole("button", { name: "Edit execution policy" }),
  );
  fireEvent.change(screen.getByLabelText("Default execution mode"), {
    target: { value: "protected" },
  });
  fireEvent.click(screen.getByLabelText(/understand.*block/i));
  fireEvent.click(
    screen.getByRole("button", { name: "Save execution policy" }),
  );
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  rerender(view("project-b"));
  await screen.findByRole("button", { name: "Edit execution policy" });
  await act(async () =>
    resolve(
      json({ ...initial, revision_id: "a-saved", default_mode: "protected" }),
    ),
  );
  expect(screen.queryByText(/execution policy saved/i)).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: "Edit execution policy" }),
  );
  expect(screen.getByLabelText("Default execution mode")).toHaveValue("direct");
});
