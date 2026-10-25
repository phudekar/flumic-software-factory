import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Failure, PageTitle, Section, date } from "../components/ui";
import { YamlInput } from "../components/yaml-input";
import { projectPath, useProjectData, useProjectId } from "../project-context";
import {
  diagnostic,
  documentObject,
  graphSource,
  useWorkflowWrite,
} from "../workflow-api";
import type {
  Definition,
  DefinitionRevision,
  WorkflowCatalog,
  ProjectRun,
} from "../workflow-api";
interface Repository {
  id: string;
  alias: string;
}
interface Pool {
  id: string;
  name: string;
  authorized: boolean;
  role: string;
  executors: string[];
  actions: string[];
}
export function WorkflowDefinitionsPage() {
  const id = useProjectId(),
    definitions = useProjectData<Definition[]>("/workflow-definitions");
  return (
    <>
      <PageTitle
        eyebrow="PROJECT WORKFLOWS"
        title="Workflow definitions"
        description="Versioned data defines stages, dependencies, bindings and builtin or Docker actions."
        action={
          <Link className="primary" to={projectPath(id, "/workflows/new")}>
            New workflow
          </Link>
        }
      />
      <Failure error={definitions.error} />
      <Section title="Published definitions">
        <div className="editor-body">
          {definitions.isPending
            ? "Loading definitions…"
            : definitions.data?.length
              ? definitions.data.map((d) => (
                  <p key={d.id}>
                    <Link to={projectPath(id, "/workflows/" + d.id)}>
                      {d.name}
                    </Link>{" "}
                    · revision {d.revision_id}
                  </p>
                ))
              : "No workflow definitions published."}
        </div>
      </Section>
    </>
  );
}
const starter = (repository: string, pool: string) => ({
  schema_version: 1,
  name: "new-workflow",
  repositories: [repository],
  default_repository: repository,
  stages: [
    {
      id: "echo",
      executor: "builtin",
      action: "core.echo@1",
      role: "simple",
      pool_id: pool,
      inputs: { message: { $ref: "input.message" } },
    },
  ],
});
export function DefinitionEditorPage() {
  const { definitionId } = useParams();
  return (
    <DefinitionEditor key={definitionId ?? "new"} identifier={definitionId} />
  );
}
function DefinitionEditor({ identifier }: { identifier?: string }) {
  const project = useProjectId(),
    navigate = useNavigate();
  const definition = useProjectData<Definition>(
    "/workflow-definitions/" + (identifier ?? ""),
    !!identifier,
  );
  const revisions = useProjectData<Omit<DefinitionRevision, "document">[]>(
    "/workflow-definitions/" + (identifier ?? "") + "/revisions",
    !!identifier,
  );
  const head = definition.data?.revision_id;
  const revision = useProjectData<DefinitionRevision>(
    "/workflow-definition-revisions/" + (head ?? ""),
    !!head,
  );
  const catalog = useProjectData<WorkflowCatalog>("/workflow-catalog");
  const repositories = useProjectData<Repository[]>("/repositories"),
    pools = useProjectData<Pool[]>("/worker-pools");
  const [draft, setDraft] = useState<{
    base: string | null;
    source: string;
    baseline: string;
  } | null>(
    identifier
      ? null
      : {
          base: null,
          source: graphSource(
            starter("choose-repository", "choose-authorized-pool"),
          ),
          baseline: "",
        },
  );
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useWorkflowWrite();
  useEffect(() => {
    if (revision.data && !pending) {
      const latest = revision.data;
      setDraft((current) =>
        !current || current.source === current.baseline
          ? {
              base: latest.id,
              source: graphSource(latest.document),
              baseline: graphSource(latest.document),
            }
          : current,
      );
    }
  }, [revision.data, pending]);
  async function submit(publish: boolean) {
    if (!draft) return;
    setError("");
    setMessage("");
    setPending(true);
    try {
      const document = documentObject(draft.source);
      if (!publish) {
        const result = await write<{ digest: string; stages: number }>(
          "/workflow-definitions/validate",
          { document },
        );
        if (alive.current)
          setMessage("Valid definition: " + result.stages + " stages.");
      } else {
        const saved = await write<Definition>(
          "/workflow-definitions" + (identifier ? "/" + identifier : ""),
          { document, ...(identifier ? { parent_id: draft.base } : {}) },
          identifier ? { method: "PUT" } : undefined,
        );
        if (alive.current) {
          setDraft({
            base: saved.revision_id,
            source: draft.source,
            baseline: draft.source,
          });
          setMessage("Published immutable revision " + saved.revision_id);
          if (!identifier)
            navigate(projectPath(project, "/workflows/" + saved.id));
        }
      }
    } catch (e) {
      if (alive.current) setError(diagnostic(e));
    } finally {
      if (alive.current) setPending(false);
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="WORKFLOW AUTHORING"
        title={
          identifier ? "Edit workflow definition" : "New workflow definition"
        }
        description="Edit YAML or JSON. Publish creates an immutable revision; accepted runs keep their pinned definition."
      />
      <Failure
        error={
          definition.error ||
          revision.error ||
          catalog.error ||
          repositories.error ||
          pools.error
        }
      />
      <Section title="Definition editor">
        <div className="editor-body">
          {draft && (
            <>
              <p>Editor baseline: {draft.base ?? "new definition"}</p>
              {head && draft.base !== head && (
                <p role="status">
                  A newer revision is available. Your draft keeps its original
                  parent.
                </p>
              )}
              <YamlInput
                label="Workflow source"
                value={draft.source}
                readOnly={pending}
                invalid={!!error}
                onChange={(source) => {
                  setDraft({ ...draft, source });
                  setMessage("");
                }}
              />
              <div className="toolbar">
                <button disabled={pending} onClick={() => void submit(false)}>
                  Validate definition
                </button>
                <button
                  className="primary"
                  disabled={pending}
                  onClick={() => void submit(true)}
                >
                  Publish revision
                </button>
                {["yaml", "json"].map((format) => (
                  <button
                    key={format}
                    disabled={pending}
                    onClick={() => {
                      try {
                        setDraft({
                          ...draft,
                          source: graphSource(
                            documentObject(draft.source),
                            format,
                          ),
                        });
                        setError("");
                      } catch (e) {
                        setError(diagnostic(e));
                      }
                    }}
                  >
                    Format {format.toUpperCase()}
                  </button>
                ))}
                {identifier ? (
                  <button
                    disabled={pending || !revision.data}
                    onClick={() => {
                      if (revision.data) {
                        const source = graphSource(revision.data.document);
                        setDraft({
                          base: revision.data.id,
                          source,
                          baseline: source,
                        });
                        setError("");
                        setMessage("");
                      }
                    }}
                  >
                    Discard edits and load latest
                  </button>
                ) : (
                  <button
                    disabled={
                      pending || repositories.isPending || pools.isPending
                    }
                    onClick={() => {
                      const pool = pools.data?.find(
                        (p) =>
                          p.authorized &&
                          p.role === "simple" &&
                          p.actions.includes("core.echo@1") &&
                          p.executors.includes("builtin@1"),
                      );
                      setDraft({
                        base: null,
                        baseline: "",
                        source: graphSource(
                          starter(
                            repositories.data?.[0]?.alias ??
                              "choose-repository",
                            pool?.id ?? "choose-authorized-pool",
                          ),
                        ),
                      });
                    }}
                  >
                    Insert starter using project repository and pool
                  </button>
                )}
              </div>
              <Failure error={error} />
              {message && <p role="status">{message}</p>}
              {draft.base && (
                <Link
                  to={projectPath(project, "/workflow-revisions/" + draft.base)}
                >
                  View or run this saved revision
                </Link>
              )}
            </>
          )}
          {!draft && <p>Loading immutable definition…</p>}
          <p>
            Link stages with <code>after: [parent-stage]</code> and inputs such
            as <code>{"value: {$ref: stages.parent-stage.output}"}</code>. The
            producer must be a direct dependency. The declared default
            repository is pinned; change it by publishing a new revision.
            Required repository aliases and pool membership must exist in this
            project.
          </p>
          <p>
            Project repositories:{" "}
            {repositories.data?.map((r) => r.alias).join(", ") || "none"}.
            Authorized pools:{" "}
            {pools.data
              ?.filter((p) => p.authorized)
              .map((p) => p.name)
              .join(", ") || "none"}
            .
          </p>
        </div>
      </Section>
      <Section title="Immutable revision history">
        <div className="editor-body">
          {revisions.data?.map((r) => (
            <p key={r.id}>
              <Link to={projectPath(project, "/workflow-revisions/" + r.id)}>
                {r.id}
              </Link>{" "}
              · parent {r.parent_id ?? "none"} · {date(r.created_at)} · digest{" "}
              {r.digest}
            </p>
          ))}
        </div>
      </Section>
      <ActionCatalog catalog={catalog.data} />
    </>
  );
}
function ActionCatalog({ catalog }: { catalog?: WorkflowCatalog }) {
  return (
    <Section title="Action catalog and schemas">
      <div className="editor-body">
        {catalog?.executors.map((e) => (
          <p key={e.id}>
            {e.id}:{" "}
            {e.available
              ? "available (" + e.version + ")"
              : "unavailable — coming soon"}
          </p>
        ))}
        {catalog?.actions.map((action) => (
          <details key={action.id}>
            <summary>
              {action.id} · role {action.role ?? "simple or developer"}
            </summary>
            <h3>Input schema</h3>
            <pre>{JSON.stringify(action.input_schema, null, 2)}</pre>
            <h3>Output schema</h3>
            <pre>{JSON.stringify(action.output_schema, null, 2)}</pre>
          </details>
        ))}
        <details>
          <summary>Full workflow definition schema</summary>
          <pre>{JSON.stringify(catalog?.definition_schema, null, 2)}</pre>
        </details>
        <p>
          Docker requires a digest-pinned image, argv command and authorized
          docker@1/docker.run@1 pool. Kubernetes and function executors are
          unavailable in this version.
        </p>
      </div>
    </Section>
  );
}
export function DefinitionRevisionPage() {
  const { revisionId } = useParams();
  return <RevisionView key={revisionId} revisionId={revisionId ?? ""} />;
}
function RevisionView({ revisionId }: { revisionId: string }) {
  const project = useProjectId(),
    revision = useProjectData<DefinitionRevision>(
      "/workflow-definition-revisions/" + revisionId,
    );
  return (
    <>
      <PageTitle
        eyebrow="IMMUTABLE WORKFLOW"
        title="Saved workflow revision"
        description="This document is read-only. Running it pins its repository/configuration context at acceptance."
      />
      <Failure error={revision.error} />
      {revision.data && (
        <>
          <p>
            Revision {revision.data.id} · digest {revision.data.digest} · parent{" "}
            {revision.data.parent_id ?? "none"}
          </p>
          <Link
            to={projectPath(
              project,
              "/workflows/" + revision.data.definition_id,
            )}
          >
            Edit current definition
          </Link>
          <YamlInput
            label="Saved workflow document"
            value={graphSource(revision.data.document)}
            readOnly
            invalid={false}
            onChange={() => {}}
          />
          <RunDefinition revision={revision.data} />
        </>
      )}
    </>
  );
}
function RunDefinition({ revision }: { revision: DefinitionRevision }) {
  const project = useProjectId(),
    navigate = useNavigate(),
    repositories = useProjectData<Repository[]>("/repositories");
  const [repository, setRepository] = useState(""),
    [input, setInput] = useState("{}"),
    [bindings, setBindings] = useState("{}"),
    [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useWorkflowWrite(),
    request = useRef<{ body: string; key: string } | null>(null);
  const ownsDefault = repositories.data?.some(
    (r) => r.alias === revision.document.default_repository,
  );
  return (
    <Section title="Run saved revision">
      <form
        className="editor-body"
        onSubmit={async (e) => {
          e.preventDefault();
          setPending(true);
          setError("");
          try {
            if (
              repository !== revision.document.default_repository ||
              !ownsDefault
            )
              throw new Error(
                "Choose the pinned default repository in this project.",
              );
            const chosenBindings = documentObject(bindings);
            for (const [repo, value] of Object.entries(chosenBindings)) {
              if (!repositories.data?.some((r) => r.id === repo))
                throw new Error(
                  "Follow-up binding repository does not belong to this project.",
                );
              if (
                !value ||
                typeof value !== "object" ||
                Array.isArray(value) ||
                Object.values(value).some((v) => typeof v !== "string")
              )
                throw new Error(
                  "Bindings map repository IDs to workflow-kind revision IDs.",
                );
            }
            const body = {
              revision_id: revision.id,
              input: documentObject(input),
              ...(Object.keys(chosenBindings).length
                ? { bindings: chosenBindings }
                : {}),
            };
            const serialized = JSON.stringify(body);
            if (request.current?.body !== serialized)
              request.current = { body: serialized, key: crypto.randomUUID() };
            const result = await write<ProjectRun>("/workflow-runs", body, {
              idempotencyKey: request.current.key,
            });
            if (alive.current)
              navigate(projectPath(project, "/runs/" + result.id));
          } catch (e) {
            if (alive.current) setError(diagnostic(e));
          } finally {
            if (alive.current) setPending(false);
          }
        }}
      >
        <p>
          Revision {revision.id}. Repository selection confirms this saved
          definition's default; it does not rewrite it.
        </p>
        <label>
          Default repository
          <select
            required
            disabled={pending || !ownsDefault}
            value={repository}
            onChange={(e) => setRepository(e.target.value)}
          >
            <option value="">Confirm repository</option>
            {ownsDefault && (
              <option value={revision.document.default_repository}>
                {revision.document.default_repository}
              </option>
            )}
          </select>
        </label>
        {!repositories.isPending && !ownsDefault && (
          <Failure error="Default repository is not connected to this project." />
        )}
        <label>
          Run input JSON
          <textarea
            disabled={pending}
            rows={5}
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
        </label>
        <label>
          Follow-up bindings JSON
          <textarea
            disabled={pending}
            rows={5}
            value={bindings}
            onChange={(e) => setBindings(e.target.value)}
          />
        </label>
        <small>
          For non-echo builtins, map repository UUIDs to
          issue/feedback/verification workflow revision UUIDs. Missing/foreign
          bindings are rejected by the server. Start the dynamic runtime service
          before expecting execution; acceptance alone is queued intent.
        </small>
        <Failure error={error || repositories.error} />
        <button
          className="primary"
          disabled={
            pending ||
            !ownsDefault ||
            repository !== revision.document.default_repository
          }
        >
          Run saved revision
        </button>
      </form>
    </Section>
  );
}
