import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { WorkerConfig, WorkerDocument } from "../api";
import { api } from "../api";
import { date, Failure, PageTitle, Section } from "../components/ui";
import { useData } from "../hooks";
import { MarkdownPromptEditor } from "../components/markdown-prompt-editor";
import { YamlEditor } from "../components/yaml-editor";
import { ImageBuildEditor } from "../components/image-build-editor";

export function WorkerDocumentEditor({
  source,
  onChange,
  readOnly = false,
}: {
  source: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
}) {
  const [task, setTask] = useState("plan_issue");
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
              readOnly={readOnly}
              value={document.tasks[selected].model ?? ""}
              placeholder="Use worker backend model"
              onChange={(e) => update("model", e.target.value)}
            />
          </label>
          <MarkdownPromptEditor
            readOnly={readOnly}
            value={document.tasks[selected].additional_system_prompt ?? ""}
            onChange={(value) => update("additional_system_prompt", value)}
          />
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
  const ordered = [...(configs.data ?? [])].sort(
    (a, b) => b.created_at - a.created_at,
  );
  const latestProfiles = [
    ...new Map(
      ordered
        .slice()
        .reverse()
        .map((c) => [c.family_id, c]),
    ).values(),
  ].sort((a, b) => a.name.localeCompare(b.name));
  const requested = ordered.find((c) => c.id === params.get("config"));
  const profileId = requested?.family_id ?? params.get("profile");
  const latest =
    latestProfiles.find((c) => c.family_id === profileId) ?? latestProfiles[0];
  const versions = ordered.filter((c) => c.family_id === latest?.family_id);
  const current = creating ? template.data : (requested ?? latest);
  const historical =
    !creating && !factoryMode && !!current && current.id !== latest?.id;
  const version =
    versions.length - versions.findIndex((c) => c.id === current?.id);
  const showHistory = params.get("history") === "1";
  const [source, setSource] = useState("");
  const [name, setName] = useState("");
  const [factoryId, setFactoryId] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const [imageError, setImageError] = useState<string | null>(null);
  const [behaviorError, setBehaviorError] = useState<string | null>(null);
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
  async function validateProfile(document: Record<string, unknown>) {
    if (!current) throw new Error("Select a worker profile first.");
    await api("/worker-profiles/validate", {
      name: name || current.name,
      profile: current.profile,
      factory_revision_id: factoryId,
      document,
    });
  }
  async function save(clone = false) {
    setPending(true);
    setError("");
    setMessage("");
    try {
      if (imageError || behaviorError)
        throw new Error("Fix the YAML configuration before saving.");
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
        setParams({ profile: saved.family_id });
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
            Worker profile
            <select
              aria-label="Worker profile"
              value={latest?.family_id ?? ""}
              onChange={(e) => {
                setParams({ profile: e.target.value });
                setError("");
                setMessage("");
              }}
            >
              {latestProfiles.map((c) => (
                <option key={c.family_id} value={c.family_id}>
                  {c.name} · v
                  {ordered.filter((r) => r.family_id === c.family_id).length}{" "}
                  (latest)
                </option>
              ))}
            </select>
          </label>
          <button
            className="secondary"
            disabled={!latest}
            onClick={() => {
              const next = new URLSearchParams(params);
              if (showHistory) next.delete("history");
              else next.set("history", "1");
              setParams(next);
            }}
          >
            {showHistory ? "Hide version history" : "Version history"}
          </button>
          {current && (
            <span className="tag">
              {historical ? "Viewing" : "Latest"} v{version} ·{" "}
              {current.id.slice(0, 8)}
            </span>
          )}
          <Link to="/workers" className="text-link">
            Create worker from saved profile →
          </Link>
        </div>
      )}
      {!factoryMode && !creating && showHistory && (
        <Section title={"Version history · " + (latest?.name ?? "")}>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Profile name</th>
                  <th>Saved</th>
                  <th>Revision</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {versions.map((revision, index) => (
                  <tr key={revision.id}>
                    <td>
                      v{versions.length - index}
                      {index === 0 ? " (latest)" : ""}
                    </td>
                    <td>{revision.name}</td>
                    <td>{date(revision.created_at)}</td>
                    <td className="mono">{revision.id.slice(0, 8)}</td>
                    <td>
                      <button
                        className="secondary"
                        onClick={() =>
                          setParams(
                            index === 0
                              ? { profile: revision.family_id, history: "1" }
                              : {
                                  profile: revision.family_id,
                                  config: revision.id,
                                  history: "1",
                                },
                          )
                        }
                      >
                        View v{versions.length - index}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      {historical && (
        <div className="notice">
          Viewing previous version v{version}. This version is read-only.{" "}
          <button
            className="secondary"
            onClick={() => setParams({ profile: latest!.family_id })}
          >
            Back to latest version
          </button>{" "}
          You can also clone this version into a new profile.
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
              readOnly={historical}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Pinned factory revision
            <select
              value={factoryId}
              disabled={historical}
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
        ) : !prompts ? (
          <YamlEditor
            key={current?.id ?? "new"}
            value={source ? JSON.parse(source) : { settings: {}, tasks: {} }}
            label="Worker behavior YAML"
            readOnly={historical}
            onChange={(value) => setSource(JSON.stringify(value, null, 2))}
            onError={setBehaviorError}
            normalize={normalizeWorkerDocument}
            onValidate={validateProfile}
          />
        ) : (
          <WorkerDocumentEditor
            source={source}
            onChange={setSource}
            readOnly={historical}
          />
        )}
      </Section>
      {!factoryMode && !prompts && (
        <Section title="File limits">
          <div className="editor-body">
            <label>
              Maximum file size (bytes)
              <input
                type="number"
                min={1}
                step={1}
                readOnly={historical}
                value={Number(settings.max_file_bytes ?? 65536)}
                onChange={(e) =>
                  updateSetting("max_file_bytes", Number(e.target.value))
                }
              />
            </label>
            <small>
              Default 65,536 bytes (64 KiB), measured as UTF-8 bytes per file.
              Applies to repository reads, coding output and final publication
              validation. Changes take effect on worker restart.
            </small>
          </div>
        </Section>
      )}
      {!factoryMode && !prompts && (
        <Section title="Worker image build">
          <div className="editor-body">
            <p>
              Build a reusable worker image from a Debian, Ubuntu or Alpine
              base. Leave the base image empty to use the existing prebuilt
              worker image. Build changes apply when creating or restarting a
              worker.
            </p>
            <ImageBuildEditor
              key={current?.id ?? "new"}
              settings={settings}
              readOnly={historical}
              onError={setImageError}
              onValidate={(build) => {
                const document = JSON.parse(source) as WorkerDocument;
                return validateProfile({
                  ...document,
                  settings: { ...document.settings, ...build },
                });
              }}
              onChange={(build) => {
                try {
                  const document = JSON.parse(source) as WorkerDocument;
                  document.settings = { ...document.settings, ...build };
                  setSource(JSON.stringify(document, null, 2));
                } catch {
                  setError("Fix configuration JSON first.");
                }
              }}
            />
            <small>
              Runs during the image build and is cached with the installed
              dependencies. Use the startup script below for commands that run
              on every start. Keep credentials out of build scripts and image
              layers.
            </small>
          </div>
        </Section>
      )}
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
                readOnly={historical}
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
                readOnly={historical}
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
          disabled={
            pending || !source || historical || !!imageError || !!behaviorError
          }
          onClick={() => save()}
        >
          {creating ? "Save worker profile" : "Save revision"}
        </button>
        {!factoryMode && !creating && (
          <button
            className="secondary"
            disabled={pending || !current || !!imageError || !!behaviorError}
            onClick={() => save(true)}
          >
            Clone as new profile
          </button>
        )}
      </div>
    </>
  );
}

function normalizeWorkerDocument(value: Record<string, unknown>) {
  if (Object.keys(value).some((key) => !["settings", "tasks"].includes(key)))
    throw new Error("Worker behavior accepts only settings and tasks.");
  for (const key of ["settings", "tasks"]) {
    if (
      !value[key] ||
      typeof value[key] !== "object" ||
      Array.isArray(value[key])
    )
      throw new Error(key + " must be a mapping.");
  }
  return value;
}
