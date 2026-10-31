// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "./main";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
function mount(path: string) {
  const fetcher = vi.fn(async (input: string) => {
    const body = input.endsWith("/auth/session")
      ? { csrf: "fixture" }
      : input.endsWith("/workers")
        ? { managed: [], discovered: [], warnings: [] }
        : [];
    return new Response(JSON.stringify(body), {
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetcher;
}
it("lands on projects with shared infrastructure and no legacy navigation", async () => {
  mount("/");
  expect(
    await screen.findByRole("heading", { name: "Projects" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("navigation", { name: "Infrastructure" }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/legacy/i)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "Worker prompts" }),
  ).not.toBeInTheDocument();
});
it("retires unscoped project pages without fetching their old APIs", async () => {
  const fetcher = mount("/workflows");
  expect(
    await screen.findByRole("heading", { name: "Choose a project" }),
  ).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith("/workflows"))).toBe(
    false,
  );
});
it("offers only shared pool provisioning on workers", async () => {
  const fetcher = mount("/workers");
  expect(
    await screen.findByRole("button", { name: "Add shared worker" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Create worker" }),
  ).not.toBeInTheDocument();
  expect(
    fetcher.mock.calls.some(([path]) => path.endsWith("/worker-profiles")),
  ).toBe(false);
});
