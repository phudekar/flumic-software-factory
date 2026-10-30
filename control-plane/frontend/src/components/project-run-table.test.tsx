// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ProjectProvider } from "../project-context";
import type { ProjectRun } from "../workflow-api";
import { localTimestamp } from "./project-format";
import { ProjectRunTable } from "./project-run-table";

const runs: ProjectRun[] = [
  {
    id: "run-12345678901234567890",
    project_id: "a/b",
    name: "factory-issue-backend-hash",
    definition_id: "d",
    revision_id: "rev",
    state: "completed",
    snapshot_id: "snap",
    created_at: 0,
    dispatch: { state: "submitted", engine_id: null, error: null },
  },
  {
    id: "other-run-1234567890",
    project_id: "a/b",
    name: "hello-world",
    definition_id: "d",
    revision_id: "rev",
    state: "failed",
    snapshot_id: "snap",
    created_at: 1793295726,
    dispatch: { state: "pending", engine_id: null, error: null },
  },
];
function mount(props: { runs: ProjectRun[]; compact?: boolean }) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ProjectProvider id="a/b">
          <ProjectRunTable {...props} />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
afterEach(cleanup);
it("shows accurate run/dispatch states, local acceptance dates and project-scoped inspection links", () => {
  mount({ runs, compact: true });
  expect(
    screen.getByRole("columnheader", { name: "Created" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Issue workflow")).toBeInTheDocument();
  expect(screen.getByText("completed")).toBeInTheDocument();
  expect(screen.getByText("submitted")).toBeInTheDocument();
  expect(screen.getByTitle(localTimestamp(0))).toBeInTheDocument();
  expect(screen.getByTitle(runs[0].id)).toHaveTextContent("run-12345678…");
  expect(
    screen.getAllByRole("link", { name: /Inspect run/ })[0],
  ).toHaveAttribute("href", "/projects/a%2Fb/runs/" + runs[0].id);
  expect(
    screen.queryByText(/pull request|business success|issue #/i),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
});
it("filters full tables by readable workflow, ID and run status", () => {
  mount({ runs });
  fireEvent.change(screen.getByRole("searchbox", { name: "Search runs" }), {
    target: { value: "Issue workflow" },
  });
  expect(screen.queryByText("hello-world")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Run status" }), {
    target: { value: "failed" },
  });
  expect(screen.getByText("hello-world")).toBeInTheDocument();
  expect(screen.queryByText("Issue workflow")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "absent" },
  });
  expect(screen.getByText("No runs match these filters.")).toBeInTheDocument();
});
it("shows a useful empty state", () => {
  mount({ runs: [] });
  expect(screen.getByText("No project runs yet.")).toBeInTheDocument();
});
