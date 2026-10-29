import { GuidedIssueInput, useIssueFollowups } from "./guided-issue-input";
import { TemplateInstallForm } from "./template-install-form";
import {
  StageSummary,
  workflowPresentation,
} from "../components/workflow-summary";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
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
  const [installOpen, setInstallOpen] = useState(false);
  const id = useProjectId(),
    definitions = useProjectData<Definition[]>("/workflow-definitions");
  return (
    <>
      <PageTitle
        eyebrow="PROJECT WORKFLOWS"
        title="Workflows"
        description="Browse what each workflow does, inspect its stages, or launch a saved version."
        action={
          <div className="toolbar">
            <button disabled={installOpen} onClick={() => setInstallOpen(true)}>
              Install templates
            </button>
            <Link className="primary" to={projectPath(id, "/workflows/new")}>
              New workflow
            </Link>
          </div>
        }
      />
      <Failure error={definitions.error} />
      {installOpen && (
        <TemplateInstallForm close={() => setInstallOpen(false)} />
      )}
      <div className="setup-guidance">
        <p>
          Set up <Link to={projectPath(id, "/repositories")}>repositories</Link>{" "}
          and <Link to={projectPath(id, "/pools")}>worker pools</Link> before
          running a workflow. Published definitions do not guarantee available
          workers.
        </p>
      </div>
      <Section title="Published workflows">
        <div className="editor-body">
          {definitions.isPending
            ? "Loading definitions…"
            : definitions.data?.length
              ? definitions.data.map((d) => (
                  <WorkflowCard key={d.id} definition={d} />
                ))
              : "No workflow definitions published."}
        </div>
      </Section>
    </>
  );
}
function WorkflowCard({ definition }: { definition: Definition }) {
  const project = useProjectId();
  const revision = useProjectData<DefinitionRevision>(
    "/workflow-definition-revisions/" + definition.revision_id,
  );
  const graph = revision.data?.document;
  const presentation = graph
    ? workflowPresentation(graph)
    : { title: definition.name, description: "Loading saved workflow…" };
  return (
    <article className="setup-card" aria-label={definition.name}>
      <div className="setup-card-heading">
        <h3>{presentation.title}</h3>
        {graph && (
          <span className="setup-badge">
            {graph.stages.length}{" "}
            {graph.stages.length === 1 ? "stage" : "stages"}
          </span>
        )}
      </div>
      {revision.isPending && <p role="status">Loading workflow summary…</p>}
      <Failure error={revision.error} />
      {graph && (
        <>
          <p>{presentation.description}</p>
          <p>
            Repositories: <strong>{graph.repositories.join(", ")}</strong>
          </p>
        </>
      )}
      <div className="toolbar">
        <Link
          to={projectPath(
            project,
            "/workflow-revisions/" + definition.revision_id,
          )}
        >
          View
        </Link>
        <Link to={projectPath(project, "/workflows/" + definition.id)}>
          Edit
        </Link>
        {graph && (
          <Link
            className="primary"
            to={
              projectPath(
                project,
                "/workflow-revisions/" + definition.revision_id,
              ) + "#run-saved-revision"
            }
          >
            Run
          </Link>
        )}
      </div>
      <details>
        <summary>Advanced details</summary>
        <p>Definition name: {definition.name}</p>
        <p>Revision: {definition.revision_id}</p>
      </details>
    </article>
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
  const client = useQueryClient();
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
      <Section title="Advanced workflow editor">
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
                    onClick={async () => {
                      setPending(true);
                      setError("");
                      setMessage("");
                      try {
                        const refreshed = await definition.refetch();
                        if (refreshed.error) throw refreshed.error;
                        if (!refreshed.data)
                          throw new Error("Definition head is unavailable.");
                        if (!alive.current) return;
                        const path =
                          "/workflow-definition-revisions/" +
                          refreshed.data.revision_id;
                        const latest =
                          await client.fetchQuery<DefinitionRevision>({
                            queryKey: ["project", project, path],
                            queryFn: ({ signal }) =>
                              api<DefinitionRevision>(
                                projectPath(project, path),
                                undefined,
                                { signal },
                              ),
                          });
                        if (alive.current) {
                          const source = graphSource(latest.document);
                          setDraft({
                            base: latest.id,
                            source,
                            baseline: source,
                          });
                        }
                      } catch (e) {
                        if (alive.current) setError(diagnostic(e));
                      } finally {
                        if (alive.current) setPending(false);
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
  const location = useLocation();
  const project = useProjectId(),
    revision = useProjectData<DefinitionRevision>(
      "/workflow-definition-revisions/" + revisionId,
    );
  useEffect(() => {
    if (revision.data && location.hash === "#run-saved-revision") {
      const target = document.getElementById("run-saved-revision");
      target?.scrollIntoView?.({ block: "start" });
    }
  }, [revision.data, location.hash]);
  return (
    <>
      <PageTitle
        eyebrow="IMMUTABLE WORKFLOW"
        title="Saved workflow revision"
        description="This document is read-only. Running it pins its repository/configuration context at acceptance."
      />
      <Failure error={revision.error} />
      {revision.isPending && <p role="status">Loading saved workflow…</p>}
      {revision.data && (
        <>
          <div className="setup-guidance">
            <h2>{workflowPresentation(revision.data.document).title}</h2>
            <p>{workflowPresentation(revision.data.document).description}</p>
            <p>
              Repositories: {revision.data.document.repositories.join(", ")}
            </p>
          </div>
          <div className="toolbar">
            <Link to={projectPath(project, "/workflows")}>All workflows</Link>
            <Link
              to={projectPath(
                project,
                "/workflows/" + revision.data.definition_id,
              )}
            >
              Edit current definition
            </Link>
          </div>
          <StageSummary graph={revision.data.document} />
          <details className="setup-advanced">
            <summary>Advanced: saved source and revision details</summary>
            <p>
              Revision {revision.data.id} · digest {revision.data.digest} ·
              parent {revision.data.parent_id ?? "none"}
            </p>
            <YamlInput
              label="Saved workflow document"
              value={graphSource(revision.data.document)}
              readOnly
              invalid={false}
              onChange={() => {}}
            />
          </details>
          <div id="run-saved-revision">
            <RunDefinition revision={revision.data} />
          </div>
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
  const guidedAvailable =
    revision.document.repositories.length === 1 &&
    revision.document.name.startsWith("factory-issue-") &&
    workflowPresentation(revision.document).title === "Issue to pull request";
  const [advanced, setAdvanced] = useState(false),
    [issue, setIssue] = useState(""),
    [feedback, setFeedback] = useState(""),
    [verify, setVerify] = useState("");
  const guided = guidedAvailable && !advanced;
  const initializedAdvanced = useRef(false);
  const followups = useIssueFollowups(
    revision.document.default_repository,
    guided ? feedback : "",
    guided ? verify : "",
    guidedAvailable,
  );
  const connection = repositories.data?.find((r) => r.alias === repository);
  const validGuided =
    Number.isSafeInteger(Number(issue)) &&
    Number(issue) > 0 &&
    followups.valid &&
    !!connection;
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
            if (guided && !validGuided)
              throw new Error(
                "Enter a positive issue number and select compatible follow-up revisions.",
              );
            const chosenBindings = guided
              ? { [connection!.id]: { issue: revision.id, feedback, verify } }
              : documentObject(bindings);
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
              input: guided
                ? { issue_number: Number(issue) }
                : documentObject(input),
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
        {guidedAvailable && (
          <label>
            Run setup
            <select
              disabled={pending}
              value={guided ? "guided" : "advanced"}
              onChange={(e) => {
                if (
                  e.target.value === "advanced" &&
                  guided &&
                  !initializedAdvanced.current
                ) {
                  initializedAdvanced.current = true;
                  setInput(
                    JSON.stringify(
                      { issue_number: Number(issue) || 0 },
                      null,
                      2,
                    ),
                  );
                  if (connection && followups.valid)
                    setBindings(
                      JSON.stringify(
                        {
                          [connection.id]: {
                            issue: revision.id,
                            feedback,
                            verify,
                          },
                        },
                        null,
                        2,
                      ),
                    );
                }
                setAdvanced(e.target.value === "advanced");
              }}
            >
              <option value="guided">Guided issue launch</option>
              <option value="advanced">Advanced JSON</option>
            </select>
          </label>
        )}
        {guidedAvailable && advanced && (
          <p>
            Advanced JSON and guided fields are separate drafts. The JSON is
            initialized only when you first open it.
          </p>
        )}
        {guided ? (
          <GuidedIssueInput
            issue={issue}
            setIssue={setIssue}
            feedback={feedback}
            setFeedback={setFeedback}
            verify={verify}
            setVerify={setVerify}
            followups={followups}
            pending={pending}
          />
        ) : (
          <>
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
              issue/feedback/verification workflow revision UUIDs.
              Missing/foreign bindings are rejected by the server. Start the
              dynamic runtime service before expecting execution; acceptance
              alone is queued intent.
            </small>
          </>
        )}
        <Failure error={error || repositories.error} />
        <button
          className="primary"
          disabled={
            pending ||
            (guided && !validGuided) ||
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
