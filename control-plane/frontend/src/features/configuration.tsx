import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { WorkerConfig, WorkerDocument } from "../api";
import { api } from "../api";
import { Failure, PageTitle, Section } from "../components/ui";
import { useData } from "../hooks";

export function WorkerDocumentEditor({
  source,
  onChange,
  prompts = false,
}: {
  source: string;
  onChange: (value: string) => void;
  prompts?: boolean;
}) {
  const [task, setTask] = useState("plan_issue");
  if (!prompts)
    return (
      <textarea
        className="config-editor"
        aria-label="Worker configuration document"
        spellCheck={false}
        value={source}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  let document: WorkerDocument;
  try {
    document = JSON.parse(source);
  } catch {
    return (
      <Failure error="Fix the configuration JSON before editing prompts." />
    );
  }
  const names = Object.keys(document.tasks ?? {});
  const selected = names.includes(task) ? task : names[0];
  function update(key: "model" | "additional_system_prompt", value: string) {
    document.tasks[selected] = {
      ...document.tasks[selected],
      [key]: key === "model" ? value || null : value,
    };
    onChange(JSON.stringify(document, null, 2));
  }
  return (
    <div className="editor-body">
      <label>
        Workflow task
        <select
          value={selected ?? ""}
          onChange={(e) => setTask(e.target.value)}
        >
          {names.map((name) => (
            <option key={name}>{name}</option>
          ))}
        </select>
      </label>
      {selected && (
        <>
          <label>
            Task model override
            <input
              value={document.tasks[selected].model ?? ""}
              placeholder="Use worker backend model"
              onChange={(e) => update("model", e.target.value)}
            />
          </label>
          <label>
            Additional system prompt
            <textarea
              className="prompt-editor"
              maxLength={16000}
              value={document.tasks[selected].additional_system_prompt ?? ""}
              onChange={(e) =>
                update("additional_system_prompt", e.target.value)
              }
            />
          </label>
          <small>
            Applies to this worker configuration only. Built-in instructions and
            output schemas remain. Steps without a model call ignore this
            prompt.
          </small>
        </>
      )}
    </div>
  );
}

export function Configuration({ prompts = false }: { prompts?: boolean }) {
  const configs = useData<WorkerConfig[]>("worker-configs", "/worker-profiles");
  const factory = useData<{ id: string; document: unknown }>(
    "factory-config",
    "/factory-config",
  );
  const revisions = useData<{ id: string; summary: string }[]>(
    "revisions",
    "/config/revisions",
  );
  const [params, setParams] = useSearchParams();
  const creating = params.get("new") === "1";
  const [archetype, setArchetype] = useState("simple");
  const template = useData<WorkerConfig>(
    "worker-template",
    "/worker-configs/template/" + archetype,
  );
  const factoryMode = !prompts && params.get("scope") === "factory";
  const current = creating
    ? template.data
    : (configs.data?.find((c) => c.id === params.get("config")) ??
      configs.data?.find((c) => c.family_id === params.get("profile")) ??
      configs.data?.[0]);
  const [source, setSource] = useState("");
  const [name, setName] = useState("");
  const [factoryId, setFactoryId] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const client = useQueryClient();
  useEffect(() => {
    setSource(
      JSON.stringify(
        factoryMode ? factory.data?.document : current?.document,
        null,
        2,
      ) ?? "",
    );
    setName(current?.name ?? "");
    setFactoryId(current?.factory_revision_id ?? "");
  }, [factoryMode, factory.data, current]);
  let settings: Record<string, unknown> = {};
  try {
    settings = (JSON.parse(source) as WorkerDocument).settings ?? {};
  } catch {
    /* JSON editor shows validation on save. */
  }
  function updateSetting(key: string, value: unknown) {
    try {
      const document = JSON.parse(source) as WorkerDocument;
      document.settings = { ...document.settings, [key]: value };
      setSource(JSON.stringify(document, null, 2));
    } catch {
      setError("Fix configuration JSON first.");
    }
  }
  async function save(clone = false) {
    setPending(true);
    setError("");
    setMessage("");
    try {
      const document = JSON.parse(source);
      if (factoryMode) {
        await api("/factory-config", {
          parent_id: factory.data?.id,
          document,
          summary: "Shared factory settings update",
        });
        await client.invalidateQueries({ queryKey: ["factory-config"] });
        await client.invalidateQueries({ queryKey: ["revisions"] });
      } else if (current) {
        const saved = await api<WorkerConfig>("/worker-profiles", {
          name:
            clone && name === current.name ? name.slice(0, 95) + " copy" : name,
          profile: current.profile,
          factory_revision_id: factoryId,
          document,
          ...(creating
            ? {}
            : clone
              ? { cloned_from: current.id }
              : { parent_id: current.id }),
        });
        await client.invalidateQueries({ queryKey: ["worker-configs"] });
        setParams({ config: saved.id });
      }
      setMessage(
        "Saved immutable revision. Linked workers pick up profile edits on their next start or restart.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="SCOPED CONFIGURATION"
        title={prompts ? "Worker profile prompts" : "Configuration"}
        description="Keep worker behavior separate from shared factory orchestration."
      />
      {!prompts && (
        <div className="tabs">
          <button
            className={!factoryMode ? "active" : ""}
            onClick={() => setParams({})}
          >
            Worker profiles
          </button>
          <button
            className={factoryMode ? "active" : ""}
            onClick={() => setParams({ scope: "factory" })}
          >
            Factory settings
          </button>
        </div>
      )}
      <Failure error={error || configs.error || factory.error} />
      {message && <div className="notice success">{message}</div>}
      <div className="notice">
        {factoryMode
          ? "Shared: repository, orchestration, state, labels, scan cron, task routing, concurrency, engine timeouts and retries. Worker configurations pin a factory revision; select the new revision in each configuration to adopt changes."
          : "Worker profiles define models, coding tools, limits, API retries, logging, capture policy and task prompts. All linked workers use the latest profile revision on their next start or restart."}
      </div>
      {!factoryMode && !creating && (
        <div className="toolbar">
          <label>
            Profile revision
            <select
              aria-label="Worker profile"
              value={current?.id ?? ""}
              onChange={(e) => setParams({ config: e.target.value })}
            >
              {configs.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} · {c.profile} · {c.id.slice(0, 8)}
                </option>
              ))}
            </select>
          </label>
          <Link to="/workers" className="text-link">
            Create worker from saved profile →
          </Link>
        </div>
      )}
      {!factoryMode && (
        <div className="toolbar">
          <button className="secondary" onClick={() => setParams({ new: "1" })}>
            New worker profile
          </button>
          {creating && (
            <label>
              Base worker type
              <select
                value={archetype}
                onChange={(e) => setArchetype(e.target.value)}
              >
                {Object.keys(
                  (factory.data?.document as { profiles?: object })?.profiles ??
                    {},
                ).map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}
      {!factoryMode && current && (
        <div className="toolbar">
          <label>
            Profile name
            <input
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Pinned factory revision
            <select
              value={factoryId}
              onChange={(e) => setFactoryId(e.target.value)}
            >
              {revisions.data?.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id.slice(0, 8)} · {r.summary}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      <Section
        title={
          factoryMode
            ? "Shared factory settings"
            : prompts
              ? "Prompts for selected worker configuration"
              : "Worker behavior"
        }
      >
        {factoryMode ? (
          <textarea
            aria-label="Factory configuration document"
            className="config-editor"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          />
        ) : (
          <WorkerDocumentEditor
            source={source}
            onChange={setSource}
            prompts={prompts}
          />
        )}
      </Section>
      {!factoryMode && !prompts && (
        <Section title="Startup script">
          <div className="editor-body">
            <p>
              Runs with /bin/sh -eu inside the worker container before task
              registration, on every start. Use idempotent commands to install
              prerequisites. The script has the container user’s permissions and
              access to its environment and mounted data. Shell exports do not
              change the worker process environment.
            </p>
            <label>
              Startup script
              <textarea
                className="prompt-editor"
                aria-label="Startup script"
                placeholder={
                  "python -m pip install --no-cache-dir -r /data/requirements.txt"
                }
                value={String(settings.startup_script ?? "")}
                onChange={(e) =>
                  updateSetting("startup_script", e.target.value)
                }
              />
            </label>
            <label>
              Startup timeout (seconds)
              <input
                type="number"
                min={1}
                max={3600}
                value={Number(settings.startup_timeout_seconds ?? 600)}
                onChange={(e) =>
                  updateSetting(
                    "startup_timeout_seconds",
                    Number(e.target.value),
                  )
                }
              />
            </label>
            <small>
              Failure or timeout prevents startup. Output is redacted and
              available in worker runtime logs. Keep credentials in the
              environment, outside the profile.
            </small>
          </div>
        </Section>
      )}
      <div className="actions">
        <button
          className="primary"
          disabled={pending || !source}
          onClick={() => save()}
        >
          {creating ? "Save worker profile" : "Save revision"}
        </button>
        {!factoryMode && !creating && (
          <button
            className="secondary"
            disabled={pending || !current}
            onClick={() => save(true)}
          >
            Clone as new profile
          </button>
        )}
      </div>
    </>
  );
}
