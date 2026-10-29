// @vitest-environment jsdom
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ProjectConfigEditor } from "./project-config-editor";
afterEach(cleanup);
const document = {
  defaults: {
    workflow_concurrency: 3,
    openai_model: "old-model",
    api_timeout_seconds: 0.5,
    future_setting: { preserve: true },
  },
  tasks: {
    plan_issue: { model: null, additional_system_prompt: "Old guidance" },
  },
  workers: { dev: { openai_model: "dev-model", future_nested: [1, 2] } },
  unrecognized: ["keep"],
};
function mount(disabled = false) {
  const changed = vi.fn();
  function Editor() {
    const [source, setSource] = useState(JSON.stringify(document));
    return (
      <ProjectConfigEditor
        disabled={disabled}
        value={source}
        onChange={(next) => {
          setSource(next);
          changed(next);
        }}
      />
    );
  }
  render(<Editor />);
  return changed;
}
it("preserves unknown fields and scopes model edits without publishing or coercing small valid timeouts", () => {
  const changed = mount();
  fireEvent.click(screen.getByRole("button", { name: "Models" }));
  const timeout = screen.getByRole("spinbutton", {
    name: /Api timeout seconds/i,
  });
  expect(timeout).not.toHaveAttribute("min", "1");
  fireEvent.change(screen.getByLabelText(/Openai model/i), {
    target: { value: "new-model" },
  });
  const next = JSON.parse(changed.mock.lastCall![0]);
  expect(next.defaults.future_setting).toEqual({ preserve: true });
  expect(next.defaults.api_timeout_seconds).toBe(0.5);
  expect(next.workers.dev.openai_model).toBe("dev-model");
  expect(next.unrecognized).toEqual(["keep"]);
  fireEvent.change(screen.getByLabelText("Configuration scope"), {
    target: { value: "dev" },
  });
  fireEvent.change(screen.getByLabelText(/Openai model/i), {
    target: { value: "worker-model" },
  });
  const scoped = JSON.parse(changed.mock.lastCall![0]);
  expect(scoped.defaults.openai_model).toBe("new-model");
  expect(scoped.workers.dev.future_nested).toEqual([1, 2]);
});
it("retains invalid source and recovers guided editing only after repair", () => {
  const changed = mount();
  fireEvent.click(screen.getByRole("button", { name: "Advanced JSON" }));
  fireEvent.change(screen.getByLabelText("Project configuration JSON"), {
    target: { value: '{"defaults":' },
  });
  fireEvent.click(screen.getByRole("button", { name: "Models" }));
  expect(screen.getByRole("alert")).toHaveTextContent("draft is retained");
  expect(screen.getByLabelText("Project configuration JSON")).toHaveValue(
    '{"defaults":',
  );
  expect(changed.mock.lastCall![0]).toBe('{"defaults":');
  fireEvent.change(screen.getByLabelText("Project configuration JSON"), {
    target: { value: JSON.stringify(document) },
  });
  expect(screen.getByLabelText(/Openai model/i)).toHaveValue("old-model");
});
it("supplements task guidance in the draft and respects disabled configuration", () => {
  const changed = mount();
  fireEvent.click(screen.getByRole("button", { name: "Prompts" }));
  fireEvent.change(screen.getByLabelText("Additional system prompt"), {
    target: { value: "Require evidence and testable results" },
  });
  expect(
    JSON.parse(changed.mock.lastCall![0]).tasks.plan_issue
      .additional_system_prompt,
  ).toBe("Require evidence and testable results");
  cleanup();
  const disabledChanged = mount(true);
  fireEvent.click(screen.getByRole("button", { name: "Models" }));
  expect(screen.getByLabelText(/Openai model/i)).toBeDisabled();
  expect(disabledChanged).not.toHaveBeenCalled();
});
