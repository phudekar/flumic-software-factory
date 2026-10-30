import { useState } from "react";
import "./project-editors.css";

type Mapping = Record<string, unknown>;
const mapping = (value: unknown): value is Mapping =>
  !!value && typeof value === "object" && !Array.isArray(value);
const fieldLabels: Record<string, string> = {
  openai_model: "OpenAI model",
  api_max_retries: "API max retries",
  api_timeout_seconds: "API timeout seconds",
  cli_timeout_seconds: "CLI timeout seconds",
};
const title = (value: string) =>
  fieldLabels[value] ??
  value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
const general: Record<
  string,
  { min?: number; max?: number; options?: string[] }
> = {
  scan_cron: {},
  workflow_concurrency: { min: 1 },
  max_patch_iterations: { min: 1, max: 20 },
  max_files_per_iteration: { min: 1, max: 5 },
  max_file_bytes: { min: 1 },
  discussion_poll_seconds: { min: 1 },
  worker_type: { options: ["simple", "developer"] },
  developer_agent: { options: ["api", "codex", "claude"] },
  log_level: { options: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] },
  log_format: { options: ["json", "text"] },
};
const models: Record<string, { min?: number; max?: number }> = {
  openai_model: {},
  codex_model: {},
  claude_model: {},
  max_context_tokens: { min: 1 },
  max_completion_tokens: { min: 1 },
  api_max_retries: { min: 0, max: 20 },
  api_timeout_seconds: { min: 0 },
  cli_timeout_seconds: { min: 1 },
};

const help: Record<string, string> = {
  scan_cron:
    "Five-field cron schedule for profile-based scans. Project repository triggers are configured separately.",
  workflow_concurrency:
    "Concurrency setting for profile-based workflows; project pool slots and runtime admission are configured separately.",
  max_patch_iterations: "Maximum patch/refinement iterations, from 1 to 20.",
  max_files_per_iteration:
    "Maximum files proposed per patch iteration, from 1 to 5.",
  max_file_bytes: "Source-file byte limit used while preparing model context.",
  discussion_poll_seconds:
    "Seconds between checks while waiting for discussion replies.",
  worker_type:
    "Worker role used by the profile. Match action roles to the authorized pool.",
  developer_agent:
    "Code-generation adapter: API model, Codex CLI or Claude CLI.",
  openai_model:
    "Model identifier for the OpenAI-compatible API adapter, unless overridden by a task.",
  codex_model: "Model identifier passed to the Codex CLI adapter.",
  claude_model:
    "Model identifier passed to Claude CLI; empty uses the CLI default.",
  max_context_tokens:
    "Context token budget. Completion tokens must be smaller than this value.",
  max_completion_tokens:
    "Output token budget for model calls; must be below the context budget.",
  api_max_retries:
    "Retries for model API requests, from 0 to 20; separate from workflow-stage retries.",
  api_timeout_seconds:
    "Positive seconds allowed for a model API request, including fractional values.",
  cli_timeout_seconds: "Seconds allowed for a model CLI invocation.",
  log_level: "Minimum application log severity.",
  log_format: "JSON for structured ingestion, or text for a terminal.",
};

/** A controlled source editor: selections never save, and every edit preserves the document. */
export function ProjectConfigEditor({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [tab, setTab] = useState("General");
  const [profile, setProfile] = useState("");
  const [task, setTask] = useState("");
  let document: Mapping = {},
    error = "";
  try {
    const parsed: unknown = JSON.parse(value);
    if (!mapping(parsed))
      throw new Error("Use a JSON object for project configuration.");
    document = parsed;
  } catch (e) {
    error = (e as Error).message;
  }
  const factoryView = mapping(document.profiles);
  const profiles = mapping(document[factoryView ? "profiles" : "workers"])
    ? (document[factoryView ? "profiles" : "workers"] as Mapping)
    : {};
  const chosen = Object.keys(profiles).includes(profile) ? profile : "";
  const path = chosen
    ? factoryView
      ? ["profiles", chosen, "settings"]
      : ["workers", chosen]
    : ["defaults"];
  const at = (keys: string[]): Mapping => {
    let current: unknown = document;
    for (const key of keys)
      current = mapping(current) ? current[key] : undefined;
    return mapping(current) ? current : {};
  };
  const settings = at(path);
  const taskPath = chosen
    ? factoryView
      ? ["profiles", chosen, "tasks"]
      : ["workers", chosen, "tasks"]
    : ["tasks"];
  const tasks = at(taskPath);
  const taskNames = Object.keys(tasks).filter((key) => mapping(tasks[key]));
  const selectedTask = taskNames.includes(task) ? task : taskNames[0];
  function update(keys: string[], next: unknown) {
    if (disabled || error) return;
    const clone = JSON.parse(value) as Mapping;
    let current = clone;
    for (const key of keys.slice(0, -1)) {
      if (!mapping(current[key])) return;
      current = current[key] as Mapping;
    }
    current[keys[keys.length - 1]] = next;
    onChange(JSON.stringify(clone, null, 2));
  }
  function fields(schema: typeof general) {
    const keys = Object.keys(schema).filter(
      (key) =>
        typeof settings[key] === "string" || typeof settings[key] === "number",
    );
    return keys.length ? (
      <div className="project-editor-fields">
        {keys.map((key) => {
          const spec = schema[key];
          const number = typeof settings[key] === "number";
          return (
            <label key={key}>
              {title(key)}
              {spec.options ? (
                <select
                  disabled={disabled}
                  value={String(settings[key])}
                  onChange={(e) => update([...path, key], e.target.value)}
                >
                  {!spec.options.includes(String(settings[key])) && (
                    <option>{String(settings[key])}</option>
                  )}
                  {spec.options.map((option) => (
                    <option key={option}>{option}</option>
                  ))}
                </select>
              ) : (
                <input
                  disabled={disabled}
                  type={number ? "number" : "text"}
                  min={spec.min}
                  max={spec.max}
                  step={
                    number
                      ? key === "api_timeout_seconds"
                        ? "any"
                        : 1
                      : undefined
                  }
                  value={String(settings[key])}
                  onChange={(e) => {
                    if (
                      !number ||
                      (e.target.value !== "" &&
                        Number.isFinite(e.target.valueAsNumber))
                    )
                      update(
                        [...path, key],
                        number ? e.target.valueAsNumber : e.target.value,
                      );
                  }}
                />
              )}
              {help[key] && <small>{help[key]}</small>}
            </label>
          );
        })}
      </div>
    ) : (
      <p>
        No explicit {tab.toLowerCase()} settings in this scope. Inherited
        settings and other fields remain in Advanced JSON.
      </p>
    );
  }
  return (
    <div className="project-config-editor">
      <div className="tabs" aria-label="Configuration sections">
        {["General", "Models", "Prompts", "Advanced JSON"].map((name) => (
          <button
            type="button"
            key={name}
            className={tab === name ? "active" : "secondary"}
            aria-pressed={tab === name}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>
      <p>
        Changes stay in this draft until you save the project configuration.
        Existing runs retain their pinned revision.
      </p>
      {error && (
        <p role="alert">
          Invalid configuration JSON: {error} Your draft is retained; repair it
          below to use guided controls.
        </p>
      )}
      {!error && tab !== "Advanced JSON" && (
        <>
          <label>
            Configuration scope
            <select
              disabled={disabled}
              value={chosen}
              onChange={(e) => setProfile(e.target.value)}
            >
              <option value="">Shared project defaults</option>
              {Object.keys(profiles).map((name) => (
                <option key={name} value={name}>
                  {title(name)} profile
                </option>
              ))}
            </select>
          </label>
          {chosen &&
            typeof at(factoryView ? ["profiles", chosen] : ["workers", chosen])
              .extends === "string" && (
              <p>
                Inherits from{" "}
                {String(
                  at(factoryView ? ["profiles", chosen] : ["workers", chosen])
                    .extends,
                )}
                . Only explicit overrides are edited here.
              </p>
            )}
          {tab === "General" && fields(general)}
          {tab === "Models" && fields(models)}
          {tab === "Prompts" &&
            (selectedTask ? (
              <>
                <label>
                  Workflow task
                  <select
                    value={selectedTask}
                    onChange={(e) => setTask(e.target.value)}
                  >
                    {taskNames.map((name) => (
                      <option key={name} value={name}>
                        {title(name)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Task model override
                  <input
                    disabled={disabled}
                    value={
                      typeof at([...taskPath, selectedTask]).model === "string"
                        ? String(at([...taskPath, selectedTask]).model)
                        : ""
                    }
                    placeholder="Use inherited model"
                    onChange={(e) =>
                      update(
                        [...taskPath, selectedTask, "model"],
                        e.target.value || null,
                      )
                    }
                  />
                </label>
                <label>
                  Additional system prompt
                  <textarea
                    disabled={disabled}
                    rows={8}
                    value={
                      typeof at([...taskPath, selectedTask])
                        .additional_system_prompt === "string"
                        ? String(
                            at([...taskPath, selectedTask])
                              .additional_system_prompt,
                          )
                        : ""
                    }
                    onChange={(e) =>
                      update(
                        [...taskPath, selectedTask, "additional_system_prompt"],
                        e.target.value,
                      )
                    }
                  />
                </label>
                <small>
                  Additional guidance supplements built-in instructions and
                  output schemas. Tasks without model calls ignore prompts.
                </small>
              </>
            ) : (
              <p>
                No explicit task overrides in this scope. Existing global tasks
                and inherited prompts remain unchanged.
              </p>
            ))}
          <details>
            <summary>Other configuration fields</summary>
            <p>These fields are preserved and editable in Advanced JSON.</p>
            <ul>
              {Object.entries(settings)
                .filter(([key]) => !(key in general) && !(key in models))
                .map(([key, item]) => (
                  <li key={key}>
                    {title(key)}:{" "}
                    {mapping(item)
                      ? `${Object.keys(item).length} nested fields`
                      : Array.isArray(item)
                        ? `${item.length} items`
                        : "Configured"}
                  </li>
                ))}
            </ul>
          </details>
        </>
      )}
      {(error || tab === "Advanced JSON") && (
        <label>
          Project configuration JSON
          <textarea
            className="config-editor"
            rows={20}
            disabled={disabled}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
        </label>
      )}
    </div>
  );
}
