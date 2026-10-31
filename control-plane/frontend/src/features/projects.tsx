import { ProjectConfigEditor } from "./project-config-editor";
import { localTimestamp } from "../components/project-format";
import { RepositoryHandover } from "./repository-handover";
import { PoolCreateForm } from "./pool-create-form";
import { ProjectOverview, ProjectHistory } from "./project-overview";
import { ProjectUsagePage } from "./usage";
import { RepositoryTriggersPage } from "./repository-triggers";
import {
  WorkflowDefinitionsPage,
  DefinitionEditorPage,
  DefinitionRevisionPage,
} from "./workflow-definitions";
import {
  ProjectRunsPage,
  ProjectRunPage,
  ProjectJobPage,
  ProjectWorkerPage,
} from "./runs";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { api, type RepositoryConnection as Repository } from "../api";
import { useData, useSetupForm } from "../hooks";
import { Failure, PageTitle, Section, Status } from "../components/ui";
import {
  ProjectProvider,
  projectPath,
  useProjectData,
  useProjectId,
  useScopedWrite,
} from "../project-context";
export interface Project {
  id: string;
  slug: string;
  name: string;
}
interface Secret {
  id: string;
  name: string;
  purpose: string;
  repository_id: string | null;
  revoked_at: number | null;
}

export function ProjectSelector() {
  const projects = useData<Project[]>("projects", "/projects");
  const location = useLocation(),
    navigate = useNavigate();
  const match = /^\/projects\/([^/]+)(\/.*)?$/.exec(location.pathname);
  return (
    <label className="project-selector">
      Project
      <select
        aria-label="Project"
        value={match?.[1] ?? ""}
        onChange={(e) =>
          navigate(
            e.target.value
              ? projectPath(e.target.value, match?.[2] ?? "/overview")
              : "/projects",
          )
        }
      >
        <option value="">Choose project</option>
        {(projects.data ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}
export function ProjectsPage() {
  const projects = useData<Project[]>("projects", "/projects");
  const [name, setName] = useState(""),
    [slug, setSlug] = useState(""),
    [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const formRef = useSetupForm(formOpen);
  const navigate = useNavigate();
  const client = useQueryClient();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return (
    <div className="project-space project-directory">
      <PageTitle
        eyebrow="PROJECTS"
        title="Projects"
        description="Organize repositories, workflows and credentials in one workspace. Workers can be shared across projects."
        action={
          <button
            className="primary"
            disabled={pending}
            onClick={() => {
              setName("");
              setSlug("");
              setError("");
              setFormOpen(true);
            }}
          >
            Add project
          </button>
        }
      />
      <Failure error={projects.error ?? error} />
      <Section title="Your projects">
        <div className="editor-body">
          {projects.isPending
            ? "Loading projects…"
            : projects.data?.map((p) => (
                <article className="setup-card" key={p.id}>
                  <div className="setup-card-heading">
                    <h3>
                      <Link to={projectPath(p.id, "/overview")}>{p.name}</Link>
                    </h3>
                    <Link
                      className="secondary"
                      to={projectPath(p.id, "/overview")}
                    >
                      Open project →
                    </Link>
                  </div>
                  <p>Manage repositories, workflows and project settings.</p>
                  <details>
                    <summary>Advanced details</summary>
                    <p>Project key: {p.slug}</p>
                    <code>{p.id}</code>
                  </details>
                </article>
              ))}
        </div>
      </Section>
      {formOpen && (
        <Section title="Create project">
          <form
            ref={formRef}
            className="editor-body"
            onSubmit={async (e) => {
              e.preventDefault();
              setPending(true);
              setError("");
              try {
                const p = await api<Project>("/projects", { name, slug });
                await client.invalidateQueries({ queryKey: ["projects"] });
                if (alive.current) navigate(projectPath(p.id, "/repositories"));
              } catch (e) {
                if (alive.current) setError((e as Error).message);
              } finally {
                if (alive.current) setPending(false);
              }
            }}
          >
            <label>
              Project name
              <input
                required
                maxLength={100}
                value={name}
                onChange={(e) => {
                  const value = e.target.value;
                  setName(value);
                  if (
                    !slug ||
                    slug ===
                      name
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, "-")
                        .replace(/^-|-$/g, "")
                  )
                    setSlug(
                      value
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, "-")
                        .replace(/^-|-$/g, ""),
                    );
                }}
              />
            </label>
            <details>
              <summary>Advanced: project key</summary>
              <p>
                Used in URLs and integrations. Generated from the name; change
                it before creating if needed.
              </p>
              <label>
                Project slug
                <input
                  required
                  pattern="[a-z][a-z0-9-]*"
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                />
              </label>
            </details>
            <div className="toolbar">
              <button className="primary" disabled={pending}>
                Create project
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => {
                  setFormOpen(false);
                  setName("");
                  setSlug("");
                  setError("");
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        </Section>
      )}
    </div>
  );
}
export function ProjectPages() {
  const { projectId } = useParams();
  return projectId ? (
    <ProjectProvider key={projectId} id={projectId}>
      <ScopedPages />
    </ProjectProvider>
  ) : null;
}
function ScopedPages() {
  const project = useProjectData<Project>("");
  return (
    <div className="project-space">
      <div className="project-heading">
        <span className="eyebrow">PROJECT WORKSPACE</span>
        <strong>{project.data?.name ?? "Loading project…"}</strong>
      </div>
      <Failure error={project.error} />
      <Routes>
        <Route index element={<Navigate to="overview" replace />} />
        <Route path="overview" element={<ProjectOverview />} />
        <Route path="history" element={<ProjectHistory />} />
        <Route path="usage" element={<ProjectUsagePage />} />
        <Route path="workflows" element={<WorkflowDefinitionsPage />} />
        <Route path="workflows/new" element={<DefinitionEditorPage />} />
        <Route
          path="workflows/:definitionId"
          element={<DefinitionEditorPage />}
        />
        <Route
          path="workflow-revisions/:revisionId"
          element={<DefinitionRevisionPage />}
        />
        <Route path="runs" element={<ProjectRunsPage />} />
        <Route path="runs/:runId" element={<ProjectRunPage />} />
        <Route path="jobs/:jobId" element={<ProjectJobPage />} />
        <Route path="workers/:workerId" element={<ProjectWorkerPage />} />
        <Route path="repositories" element={<Repositories />} />
        <Route path="triggers" element={<RepositoryTriggersPage />} />
        <Route path="credentials" element={<Credentials />} />
        <Route path="config" element={<ProjectConfiguration />} />
        <Route path="pools" element={<ProjectPools />} />
        <Route
          path="*"
          element={<p>Choose a project page from the sidebar.</p>}
        />
      </Routes>
    </div>
  );
}
function object(source: string) {
  const value = JSON.parse(source);
  if (!value || Array.isArray(value) || typeof value !== "object")
    throw new Error("Use a JSON object.");
  return value;
}
function Repositories() {
  const repositories = useProjectData<Repository[]>("/repositories"),
    secrets = useProjectData<Secret[]>("/secrets");
  const [selected, setSelected] = useState<Repository | null>(null),
    [provider, setProvider] = useState<"github" | "gitlab">("github"),
    [pipelineSources, setPipelineSources] = useState("push"),
    [statusNames, setStatusNames] = useState(""),
    [alias, setAlias] = useState(""),
    [owner, setOwner] = useState(""),
    [name, setName] = useState(""),
    [credential, setCredential] = useState(""),
    [overrides, setOverrides] = useState("{}"),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useScopedWrite();
  const project = useProjectId();
  const [formOpen, setFormOpen] = useState(false);
  const formRef = useSetupForm(formOpen, selected?.id);
  function edit(repo: Repository | null) {
    setFormOpen(true);
    setSelected(repo);
    setProvider(repo?.provider ?? "github");
    setPipelineSources(
      (repo?.check_policy?.required_pipeline_sources ?? ["push"]).join(", "),
    );
    setStatusNames(
      (repo?.check_policy?.required_status_names ?? []).join(", "),
    );
    setAlias(repo?.alias ?? "");
    setOwner(repo?.owner ?? "");
    setName(repo?.name ?? "");
    setCredential(repo?.credential_id ?? "");
    setOverrides(JSON.stringify(repo?.overrides ?? {}, null, 2));
    setMessage("");
    setError("");
  }
  return (
    <>
      <PageTitle
        eyebrow="PROJECT REPOSITORIES"
        title="Repositories"
        description="Connect the source repositories your workflows use. Each connection belongs to this project."
        action={
          <button
            className="primary"
            disabled={pending}
            onClick={() => edit(null)}
          >
            Add repository
          </button>
        }
      />
      <Failure error={error || repositories.error || secrets.error} />
      <Section title="Connections">
        <div className="editor-body">
          {repositories.isPending
            ? "Loading repositories…"
            : repositories.data?.length
              ? repositories.data.map((r) => (
                  <article className="setup-card" key={r.id}>
                    <div className="setup-card-heading">
                      <h3>
                        {r.owner}/{r.name}
                      </h3>
                      <span className="setup-badge">
                        {r.provider === "gitlab" ? "GitLab" : "GitHub"}
                      </span>
                    </div>
                    <p>
                      Repository alias: <strong>{r.alias}</strong>
                    </p>
                    <p>
                      {r.credential_id
                        ? `Credential: ${secrets.isPending ? "Loading…" : (secrets.data?.find((s) => s.id === r.credential_id)?.name ?? "Reference unavailable")}`
                        : "Missing credential"}
                    </p>
                    <p>
                      {r.credential_id
                        ? "Assignment does not verify connectivity."
                        : "Add a source credential before running repository actions."}
                    </p>
                    {r.provider === "gitlab" && (
                      <p>
                        {r.identity?.project_id
                          ? "GitLab identity validated"
                          : "GitLab identity needs validation"}
                      </p>
                    )}
                    <div className="toolbar">
                      <button
                        disabled={pending}
                        aria-label={`Edit ${r.alias}`}
                        onClick={() => edit(r)}
                      >
                        Edit connection
                      </button>
                    </div>
                    <details>
                      <summary>Advanced details</summary>
                      <p>Connection ID: {r.id}</p>
                      <p>Revision: {r.revision_id}</p>
                    </details>
                    <RepositoryHandover key={r.revision_id} repository={r} />
                  </article>
                ))
              : "No repositories connected."}
          <p>
            <Link to={projectPath(project, "/credentials")}>
              Manage credentials
            </Link>{" "}
            ·{" "}
            <Link to={projectPath(project, "/workflows")}>
              Browse workflows
            </Link>
          </p>
        </div>
      </Section>
      {message && (
        <p role="status" className="notice">
          {message}
        </p>
      )}
      {formOpen && (
        <Section title={selected ? "Edit connection" : "Connect repository"}>
          <form
            ref={formRef}
            className="editor-body"
            onSubmit={async (e) => {
              e.preventDefault();
              setPending(true);
              setError("");
              setMessage("");
              try {
                await write(
                  "/repositories" + (selected ? "/" + selected.id : ""),
                  {
                    alias,
                    ...(provider === "gitlab"
                      ? {
                          provider,
                          host: "gitlab.com",
                          check_policy: {
                            required_pipeline_sources: pipelineSources
                              .split(",")
                              .map((v) => v.trim())
                              .filter(Boolean),
                            required_status_names: statusNames
                              .split(",")
                              .map((v) => v.trim())
                              .filter(Boolean),
                          },
                        }
                      : {}),
                    owner,
                    name,
                    credential_id: credential || null,
                    overrides: object(overrides),
                    ...(selected ? { parent_id: selected.revision_id } : {}),
                  },
                  selected ? "PUT" : undefined,
                );
                if (alive.current) {
                  setFormOpen(false);
                  setSelected(null);
                  setMessage("Repository saved.");
                }
              } catch (e) {
                if (alive.current) setError((e as Error).message);
              } finally {
                if (alive.current) setPending(false);
              }
            }}
          >
            <label>
              Source provider
              <select
                value={provider}
                disabled={!!selected || pending}
                onChange={(e) => {
                  setProvider(e.target.value as "github" | "gitlab");
                  setCredential("");
                }}
              >
                <option value="github">GitHub.com</option>
                <option value="gitlab">GitLab.com</option>
              </select>
            </label>
            <p>
              Choose a short alias, such as backend. Workflows use this name to
              select the repository.
            </p>
            <label>
              Repository alias
              <input
                required
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
              />
            </label>
            <label>
              {provider === "gitlab" ? "GitLab namespace" : "GitHub owner"}
              <input
                required
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
              />
            </label>
            <label>
              Repository name
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label>
              {provider === "gitlab"
                ? "GitLab credential reference"
                : "GitHub credential reference"}
              <select
                value={credential}
                onChange={(e) => setCredential(e.target.value)}
              >
                <option value="">No credential</option>
                {secrets.data
                  ?.filter(
                    (s) =>
                      s.purpose === provider &&
                      !s.revoked_at &&
                      (!s.repository_id || s.repository_id === selected?.id),
                  )
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
              </select>
            </label>
            {provider === "gitlab" && (
              <>
                <p>
                  GitLab.com only. Nested namespaces such as group/subgroup are
                  supported.
                </p>
                <label>
                  Required pipeline sources
                  <input
                    value={pipelineSources}
                    onChange={(e) => setPipelineSources(e.target.value)}
                  />
                </label>
                <label>
                  Required external statuses
                  <input
                    value={statusNames}
                    onChange={(e) => setStatusNames(e.target.value)}
                  />
                </label>
                <small>
                  Comma-separated names. Default: push pipeline. Every required
                  source and named status must succeed for the exact commit and
                  branch.
                </small>
                {selected && (
                  <>
                    <p>
                      {selected.identity?.project_id
                        ? `GitLab project ID: ${selected.identity.project_id}`
                        : "GitLab identity is not validated. Save a source credential, then validate before launching."}
                    </p>
                    <button
                      type="button"
                      disabled={pending}
                      onClick={async () => {
                        setPending(true);
                        setError("");
                        setMessage("");
                        try {
                          const validated = await write<Repository>(
                            "/repositories/" + selected.id + "/validate",
                            { parent_id: selected.revision_id },
                          );
                          if (alive.current) {
                            edit(validated);
                            setMessage("GitLab identity validated.");
                          }
                        } catch (e) {
                          if (alive.current) setError((e as Error).message);
                        } finally {
                          if (alive.current) setPending(false);
                        }
                      }}
                    >
                      Validate GitLab identity
                    </button>
                    <small>
                      Validation reads the saved revision. Save form changes
                      first. A renamed path must still resolve to the same
                      numeric project.
                    </small>
                  </>
                )}
              </>
            )}
            <details>
              <summary>Advanced: behavior overrides</summary>
              <p>
                Optional JSON settings for this repository. Leave unchanged to
                inherit project defaults.
              </p>
              <label>
                Repository behavior overrides (JSON)
                <textarea
                  rows={8}
                  value={overrides}
                  onChange={(e) => setOverrides(e.target.value)}
                />
              </label>
              <small>
                Example:{" "}
                {`{"openai_model":"gpt-4.1", "tasks":{"plan_issue":{"retries":2}}}`}
                . Repository identity and credentials are separate from
                behavior.
              </small>
            </details>
            <div className="toolbar">
              <button
                className="primary"
                disabled={pending || repositories.isPending}
              >
                Save repository
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => {
                  edit(null);
                  setFormOpen(false);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        </Section>
      )}
      <details className="setup-advanced">
        <summary>Advanced: preview effective configuration</summary>
        <EffectivePreview repositories={repositories.data ?? []} />
      </details>
    </>
  );
}
interface EffectiveResult {
  repository_id: string;
  repository_revision_id: string;
  factory_revision_id: string;
  settings: unknown;
}
function EffectivePreview({ repositories }: { repositories: Repository[] }) {
  const revisions = useProjectData<{ id: string }[]>("/config/revisions");
  const head = revisions.data?.[0]?.id;
  const [repositoryId, setRepositoryId] = useState("");
  const [profile, setProfile] = useState("simple");
  const [preview, setPreview] = useState<{
    identity: string;
    result: EffectiveResult;
  } | null>(null);
  const [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useScopedWrite();
  const selected =
    repositories.find((r) => r.id === repositoryId) ??
    (!repositoryId ? repositories[0] : undefined);
  useEffect(() => {
    setRepositoryId((current) =>
      repositories.some((r) => r.id === current)
        ? current
        : (repositories[0]?.id ?? ""),
    );
  }, [repositories]);
  const identity = JSON.stringify([
    selected?.id,
    selected?.revision_id,
    profile,
    head,
  ]);
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  return (
    <Section title="Effective configuration preview">
      <form
        className="editor-body"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!selected) return;
          const requestedIdentity = identity;
          setPending(true);
          setError("");
          setPreview(null);
          try {
            const result = await write<EffectiveResult>("/config/effective", {
              repository: selected.alias,
              profile,
            });
            if (
              alive.current &&
              currentIdentity.current === requestedIdentity
            ) {
              if (
                result.repository_id !== selected.id ||
                result.repository_revision_id !== selected.revision_id ||
                (head && result.factory_revision_id !== head)
              ) {
                setError(
                  "Saved configuration changed during preview. Refresh and try again.",
                );
              } else setPreview({ identity: requestedIdentity, result });
            }
          } catch (e) {
            if (alive.current && currentIdentity.current === requestedIdentity)
              setError((e as Error).message);
          } finally {
            if (alive.current) setPending(false);
          }
        }}
      >
        <label>
          Preview repository
          <select
            disabled={pending}
            value={selected?.id ?? ""}
            onChange={(e) => {
              setRepositoryId(e.target.value);
              setPreview(null);
              setError("");
            }}
          >
            {repositories.map((r) => (
              <option key={r.id} value={r.id}>
                {r.alias}
              </option>
            ))}
          </select>
        </label>
        <label>
          Preview profile
          <input
            disabled={pending}
            value={profile}
            onChange={(e) => {
              setProfile(e.target.value);
              setPreview(null);
              setError("");
            }}
          />
        </label>
        <button disabled={!selected || pending || revisions.isPending}>
          Preview effective configuration
        </button>
        <Failure error={error || revisions.error} />
        {preview?.identity === identity && (
          <pre>{JSON.stringify(preview.result, null, 2)}</pre>
        )}
      </form>
    </Section>
  );
}
function Credentials() {
  const secrets = useProjectData<Secret[]>("/secrets"),
    repositories = useProjectData<Repository[]>("/repositories");
  const { write, alive } = useScopedWrite();
  const [name, setName] = useState(""),
    [purpose, setPurpose] = useState("github"),
    [value, setValue] = useState(""),
    [repository, setRepository] = useState(""),
    [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const formRef = useSetupForm(formOpen);
  const [message, setMessage] = useState("");
  return (
    <>
      <PageTitle
        eyebrow="PROJECT CREDENTIALS"
        title="Credentials"
        description="Save access tokens and API keys for this project. Secret values are encrypted and never displayed again."
        action={
          <button
            className="primary"
            disabled={pending}
            onClick={() => {
              setFormOpen(true);
              setMessage("");
              setError("");
            }}
          >
            Add credential
          </button>
        }
      />
      <Failure error={error || secrets.error || repositories.error} />
      <Section title="Saved credentials">
        {secrets.isPending && (
          <p className="editor-body">Loading credentials…</p>
        )}
        {!secrets.isPending && !secrets.error && !secrets.data?.length && (
          <p className="editor-body">
            No credentials saved. Add a source token or model API key to get
            started.
          </p>
        )}
        <div className="editor-body">
          {secrets.data?.map((s) => (
            <article className="setup-card" key={s.id}>
              <div className="setup-card-heading">
                <h3>{s.name}</h3>
                <span className="setup-badge">{s.purpose}</span>
              </div>
              <p>
                Scope:{" "}
                {s.repository_id
                  ? (repositories.data?.find((r) => r.id === s.repository_id)
                      ?.alias ?? "Repository-scoped")
                  : "Whole project"}
              </p>
              <p>
                {s.revoked_at != null
                  ? "Revoked · " + localTimestamp(s.revoked_at)
                  : "Saved · value hidden"}
                . Saving a key does not verify provider access.
              </p>
              {s.revoked_at ? (
                "revoked"
              ) : (
                <button
                  disabled={pending}
                  onClick={async () => {
                    setPending(true);
                    setError("");
                    try {
                      await write("/secrets/" + s.id + "/revoke", {});
                    } catch (e) {
                      if (alive.current) setError((e as Error).message);
                    } finally {
                      if (alive.current) setPending(false);
                    }
                  }}
                >
                  Revoke {s.name}
                </button>
              )}
              <details>
                <summary>Advanced details</summary>
                <p>Credential reference: {s.id}</p>
              </details>
            </article>
          ))}
        </div>
      </Section>
      {message && (
        <p role="status" className="notice">
          {message}
        </p>
      )}
      {formOpen && (
        <Section title="Add credential">
          <form
            ref={formRef}
            className="editor-body"
            onSubmit={async (e) => {
              e.preventDefault();
              setPending(true);
              setError("");
              try {
                await write("/secrets", {
                  name,
                  purpose,
                  value,
                  repository_id: repository || null,
                });
                if (alive.current) {
                  setValue("");
                  setName("");
                  setFormOpen(false);
                  setMessage(
                    "Credential saved. Its value will not be shown again.",
                  );
                }
              } catch (e) {
                if (alive.current) setError((e as Error).message);
              } finally {
                if (alive.current) setPending(false);
              }
            }}
          >
            <label>
              Credential name
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label>
              Purpose
              <select
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
              >
                {["github", "gitlab", "openai", "anthropic"].map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
            <label>
              Credential repository
              <select
                value={repository}
                onChange={(e) => setRepository(e.target.value)}
              >
                <option value="">Whole project</option>
                {repositories.data?.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.alias}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Secret value
              <input
                required
                type="password"
                autoComplete="new-password"
                value={value}
                onChange={(e) => setValue(e.target.value)}
              />
            </label>
            <div className="toolbar">
              <button className="primary" disabled={pending}>
                Create credential
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => {
                  setValue("");
                  setName("");
                  setPurpose("github");
                  setRepository("");
                  setError("");
                  setFormOpen(false);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        </Section>
      )}
    </>
  );
}
interface ConfigRevision {
  id: string;
  document: unknown;
}
interface ConfigDraft {
  id: string;
  baseline: string;
  source: string;
}
function draftFor(revision: ConfigRevision): ConfigDraft {
  const source = JSON.stringify(revision.document, null, 2);
  return { id: revision.id, baseline: source, source };
}
export function ProjectConfiguration() {
  const revisions =
    useProjectData<{ id: string; summary: string; created_at?: number }[]>(
      "/config/revisions",
    );
  const head = revisions.data?.[0]?.id;
  const revision = useProjectData<ConfigRevision>(
    "/config/revisions/" + (head ?? ""),
    !!head,
  );
  const [draft, setDraft] = useState<ConfigDraft | null>(null);
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useScopedWrite();
  useEffect(() => {
    if (revision.data && !pending) {
      const latest = revision.data;
      setDraft((current) =>
        !current || current.source === current.baseline
          ? draftFor(latest)
          : current,
      );
    }
  }, [revision.data, pending]);
  const changed = !!draft && !!head && draft.id !== head;
  return (
    <>
      <PageTitle
        eyebrow="PROJECT BEHAVIOR"
        title="Project configuration"
        description="Project defaults merge with repository overrides, then workflow and stage overrides. New runs pin immutable revisions."
      />
      <Failure error={error || revisions.error || revision.error} />
      <Section title="Project defaults">
        <form
          className="editor-body"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!draft) return;
            setPending(true);
            setError("");
            setMessage("");
            try {
              const saved = await write<ConfigRevision>("/config/revisions", {
                parent_id: draft.id,
                document: object(draft.source),
                summary: "Project defaults updated from Control Pane",
              });
              if (alive.current) {
                setDraft(draftFor(saved));
                setMessage("Project configuration saved.");
              }
            } catch (e) {
              if (alive.current) setError((e as Error).message);
            } finally {
              if (alive.current) setPending(false);
            }
          }}
        >
          {changed && (
            <p role="status">
              A newer configuration revision is available. Your draft retains
              its original baseline; saving may conflict. Load the latest
              revision deliberately to discard these edits.
            </p>
          )}
          {draft ? (
            <ProjectConfigEditor
              disabled={pending || !draft}
              value={draft?.source ?? ""}
              onChange={(source) =>
                setDraft((current) => (current ? { ...current, source } : null))
              }
            />
          ) : (
            <p role="status">
              {revisions.isPending || (!!head && revision.isPending)
                ? "Loading project defaults…"
                : "No saved project configuration is available."}
            </p>
          )}
          <button disabled={pending || !draft}>
            Save project configuration
          </button>
          {(changed || draft?.source !== draft?.baseline) && (
            <button
              type="button"
              disabled={pending || !revision.data}
              onClick={() => {
                if (revision.data) {
                  setDraft(draftFor(revision.data));
                  setError("");
                  setMessage("");
                }
              }}
            >
              Discard edits and load latest
            </button>
          )}
          {message && <p role="status">{message}</p>}
          <small>
            Editor baseline: {draft?.id ?? "unavailable"}. Latest revision:{" "}
            {head ?? "unavailable"}. Provider grants are selected by workflow
            action bindings using the project credential IDs. Running actions
            keep their pinned settings.
          </small>
        </form>
      </Section>
      <Section title="Saved revisions">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Revision</th>
                <th>Change summary</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {revisions.data?.map((r) => (
                <tr key={r.id}>
                  <td>
                    <code title={r.id}>
                      {r.id.slice(0, 12)}
                      {r.id.length > 12 ? "…" : ""}
                    </code>
                  </td>
                  <td>{r.summary || "No summary"}</td>
                  <td>
                    <time>{localTimestamp(r.created_at)}</time>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
interface Pool {
  hatchet_tls_strategy: string;
  id: string;
  name: string;
  role: string;
  normal_slots: number;
  durable_slots: number;
  actions: string[];
  executors: string[];
  authorized: boolean;
  workers: {
    id: string;
    name: string;
    protocol: string;
    desired_state: string;
    capability_state: string;
    missing_actions: string[];
    missing_executors: string[];
    capabilities: { actions?: string[]; executors?: string[] };
    last_heartbeat_at: number | null;
    registered_at?: number | null;
  }[];
}
function ProjectPools() {
  const pools = useProjectData<Pool[]>("/worker-pools");
  const { write, alive } = useScopedWrite();
  const [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [message, setMessage] = useState("");
  return (
    <>
      <PageTitle
        eyebrow="SHARED INFRASTRUCTURE"
        title="Shared worker pools"
        action={
          <button
            className="primary"
            disabled={pending || formOpen}
            onClick={() => {
              setMessage("");
              setFormOpen(true);
            }}
          >
            Add pool
          </button>
        }
        description="Pool capabilities and slots belong to deployment infrastructure. Membership authorizes this project; it does not select repository behavior."
      />
      <Failure error={error || pools.error} />
      {message && <p role="status">{message}</p>}
      {formOpen && (
        <PoolCreateForm
          onCancel={() => setFormOpen(false)}
          onCreated={(name) => {
            setFormOpen(false);
            setMessage(
              `Pool ${name} created. Authorize this project before adding a shared worker.`,
            );
          }}
        />
      )}
      <p>
        Registered capabilities show what an image loaded. Heartbeats and
        desired state do not guarantee available runtime capacity. Revision
        support is checked at execution; missing action/executor versions cannot
        run.
      </p>
      {pools.isPending && <p>Loading pools…</p>}
      {pools.data?.length === 0 && <p>No shared pools configured.</p>}
      {pools.data?.map((p) => (
        <Section key={p.id} title={p.name}>
          <div className="editor-body">
            <PoolEligibility pool={p} />
            <details>
              <summary>Advanced pool details</summary>
              <p>
                Pool ID: <code>{p.id}</code>
              </p>
              <p>Hatchet transport: {p.hatchet_tls_strategy}</p>
            </details>
            <p>
              {p.role} · {p.normal_slots} normal / {p.durable_slots} durable
              slots per worker
            </p>
            <details className="setup-advanced">
              <summary>Supported actions and executors</summary>
              <p>
                Executors: {p.executors.join(", ")}
                <br />
                Action versions: {p.actions.join(", ")}
              </p>
            </details>
            <button
              disabled={pending}
              onClick={async () => {
                setPending(true);
                setError("");
                try {
                  await write(
                    "/worker-pools/" + p.id,
                    { enabled: !p.authorized },
                    "PUT",
                  );
                } catch (e) {
                  if (alive.current) setError((e as Error).message);
                } finally {
                  if (alive.current) setPending(false);
                }
              }}
            >
              {p.authorized
                ? "Remove project authorization"
                : "Authorize project"}
            </button>
            <p>
              {p.authorized ? "Project authorized" : "Project not authorized"}
            </p>
            {p.workers.length === 0 ? (
              <p>No workers in this pool.</p>
            ) : (
              <div className="project-pool-workers">
                {p.workers.map((w) => (
                  <div className="project-pool-worker" key={w.id}>
                    <div className="setup-card-heading">
                      <strong>{w.name}</strong>
                      <Status value={w.capability_state} />
                    </div>
                    <p>Desired state: {w.desired_state}</p>
                    <p>Last heartbeat: {localTimestamp(w.last_heartbeat_at)}</p>
                    <p>Registered: {localTimestamp(w.registered_at)}</p>
                    <details className="setup-advanced">
                      <summary>Capabilities and diagnostics</summary>
                      <p>
                        Missing action versions:{" "}
                        {w.missing_actions.join(", ") || "none"}
                      </p>
                      <p>
                        Missing executor versions:{" "}
                        {w.missing_executors.join(", ") || "none"}
                      </p>
                      <p>
                        Loaded actions:{" "}
                        {w.capabilities.actions?.join(", ") || "unknown"}
                        <br />
                        Loaded executors:{" "}
                        {w.capabilities.executors?.join(", ") || "unknown"}
                        <br />
                        Protocol: {w.protocol}
                      </p>
                    </details>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Section>
      ))}
    </>
  );
}

function PoolEligibility({ pool: p }: { pool: Pool }) {
  const [action, setAction] = useState(p.actions[0] ?? ""),
    [executor, setExecutor] = useState(p.executors[0] ?? "");
  const workers = p.authorized
    ? p.workers.filter(
        (w) =>
          ["loaded", "partial"].includes(w.capability_state) &&
          !["stopped", "removed"].includes(w.desired_state) &&
          w.capabilities.actions?.includes(action) &&
          w.capabilities.executors?.includes(executor),
      )
    : [];
  return (
    <div className="project-pool-eligibility">
      <label>
        Required action in {p.name}
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          {p.actions.map((a) => (
            <option key={a}>{a}</option>
          ))}
        </select>
      </label>
      <label>
        Required executor in {p.name}
        <select value={executor} onChange={(e) => setExecutor(e.target.value)}>
          {p.executors.map((a) => (
            <option key={a}>{a}</option>
          ))}
        </select>
      </label>
      <p>
        Eligible registered slots: {workers.length * p.normal_slots} normal /{" "}
        {workers.length * p.durable_slots} durable
      </p>
      <small>
        Configured capacity for this capability pair. Runtime availability and
        occupied slots are unknown.
      </small>
    </div>
  );
}
