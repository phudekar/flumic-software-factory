import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, Check, Plus, Workflow } from "lucide-react";
import { useEffect, useState } from "react";
import type { Config, Revision } from "../api";
import { api } from "../api";
import { Failure, PageTitle, Section } from "../components/ui";
import { useData } from "../hooks";

export function Configuration({ prompts = false }: { prompts?: boolean }) {
  const revisions = useData<Revision[]>("revisions", "/config/revisions");
  const [revisionId, setRevisionId] = useState("");
  const currentId = revisionId || revisions.data?.[0]?.id;
  const revision = useQuery<Revision>({
    queryKey: ["revision", currentId],
    queryFn: () => api("/config/revisions/" + currentId),
    enabled: !!currentId,
    refetchInterval: false,
  });
  const [source, setSource] = useState("");
  const [profile, setProfile] = useState("simple");
  const [task, setTask] = useState("plan_issue");
  const [prompt, setPrompt] = useState("");
  const [summary, setSummary] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const client = useQueryClient();
  useEffect(() => {
    if (revision.data)
      setSource(JSON.stringify(revision.data.document, null, 2));
  }, [revision.data]);
  const selectedTask = revision.data?.profiles[profile]?.tasks[task];
  useEffect(() => {
    setPrompt(selectedTask?.additional_system_prompt ?? "");
  }, [selectedTask]);
  function document(): Config {
    const value = JSON.parse(source) as Config;
    if (prompts) {
      value.tasks[task] = {
        ...value.tasks[task],
        additional_system_prompt: prompt,
      };
    }
    return value;
  }
  async function save(publish: boolean) {
    setPending(true);
    setError("");
    setMessage("");
    try {
      const payload = {
        document: document(),
        parent_id: currentId,
        summary: summary || `${prompts ? "Prompt" : "Configuration"} update`,
      };
      if (publish) {
        const saved = await api<Revision>("/config/revisions", payload);
        setRevisionId(saved.id);
        setMessage(
          "New immutable revision saved. Running workers are unchanged. Stop a managed worker and use its configuration action to apply this revision.",
        );
        await client.invalidateQueries({ queryKey: ["revisions"] });
        await client.invalidateQueries({ queryKey: ["revision"] });
      } else {
        await api("/config/validate", payload);
        setMessage(
          "All profiles and task settings are valid. Preview makes no model calls.",
        );
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="VERSIONED FACTORY SETTINGS"
        title={prompts ? "Task prompts" : "Configuration"}
        description={
          prompts
            ? "Tune task guidance while preserving built-in instructions and response schemas."
            : "Validate and save immutable configuration revisions. Running workers keep their loaded snapshot."
        }
        action={
          <div className="actions">
            <button
              className="secondary"
              disabled={pending || !revision.data}
              onClick={() => save(false)}
            >
              <Check size={14} />
              Validate
            </button>
            <button
              className="primary"
              disabled={pending || !revision.data}
              onClick={() => save(true)}
            >
              <Plus size={14} />
              Save revision
            </button>
          </div>
        }
      />
      <Failure error={error || revision.error} />
      {message && <div className="notice success">{message}</div>}
      <div className="toolbar">
        <label className="inline-label">
          Revision
          <select
            value={currentId ?? ""}
            onChange={(e) => {
              if (
                source !== JSON.stringify(revision.data?.document, null, 2) &&
                !window.confirm("Discard unsaved edits?")
              )
                return;
              setRevisionId(e.target.value);
              setMessage("");
            }}
          >
            {revisions.data?.map((r) => (
              <option key={r.id} value={r.id}>
                {r.id.slice(0, 8)} · {r.summary}
              </option>
            ))}
          </select>
        </label>
        {currentId && (
          <a
            className="text-link"
            href={"/api/v1/config/revisions/" + currentId + "/export"}
          >
            Export YAML <ArrowUpRight size={14} />
          </a>
        )}
      </div>
      {prompts ? (
        <div className="prompt-layout">
          <Section title="Workflow tasks">
            <div className="task-list">
              {Object.keys(revision.data?.document.tasks ?? {}).map((name) => (
                <button
                  key={name}
                  className={name === task ? "selected" : ""}
                  onClick={() => {
                    if (
                      prompt !==
                        (selectedTask?.additional_system_prompt ?? "") &&
                      !window.confirm("Discard unsaved prompt edits?")
                    )
                      return;
                    setTask(name);
                  }}
                >
                  <Workflow size={14} />
                  {name.replaceAll("_", " ")}
                </button>
              ))}
            </div>
          </Section>
          <Section
            title={task.replaceAll("_", " ")}
            aside={<span className="tag">Additional system prompt</span>}
          >
            <div className="editor-body">
              <label>
                Preview profile
                <select
                  value={profile}
                  onChange={(e) => setProfile(e.target.value)}
                >
                  {Object.keys(revision.data?.profiles ?? {}).map((name) => (
                    <option key={name}>{name}</option>
                  ))}
                </select>
              </label>
              <p className="muted">
                Task override replaces inherited template guidance. Built-in
                instructions are added by the worker image.
              </p>
              <textarea
                className="prompt-editor"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                aria-label="Additional system prompt"
              />
              <div className="prompt-meta">
                <span className="tag">
                  {selectedTask?.model ||
                    revision.data?.profiles[profile]?.model ||
                    "Backend default"}
                </span>
                <span className="tag">{selectedTask?.worker_type}</span>
                <span className="tag">
                  {prompt.length.toLocaleString()} / 16,000 characters
                </span>
              </div>
              <small>
                Steps that do not call a model ignore this field. No provider
                request is made by this editor.
              </small>
            </div>
          </Section>
        </div>
      ) : (
        <Section
          title="Resolved configuration"
          aside={<span className="tag">JSON editor · YAML export</span>}
        >
          <textarea
            className="config-editor"
            spellCheck={false}
            aria-label="Configuration document"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          />
        </Section>
      )}
      <label className="change-summary">
        Change summary
        <input
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          placeholder="Describe why this configuration is changing…"
          maxLength={240}
        />
      </label>
      <div className="notice">
        Saving creates a revision without modifying repository files. Export
        YAML for Git review. Worker replacement is explicit; automatic rolling
        deployment and rollback orchestration are not enabled yet.
      </div>
    </>
  );
}
