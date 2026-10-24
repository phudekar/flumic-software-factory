import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { api } from "../api";
import { useData } from "../hooks";
import { Failure, PageTitle, Section } from "../components/ui";
import {
  ProjectProvider,
  projectPath,
  useProjectData,
  useProjectId,
} from "../project-context";
export interface Project {
  id: string;
  slug: string;
  name: string;
}
interface Repository {
  id: string;
  alias: string;
  owner: string;
  name: string;
  revision_id: string;
  credential_id?: string | null;
  overrides: Record<string, unknown>;
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
              ? projectPath(e.target.value, match?.[2] ?? "/repositories")
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
  const navigate = useNavigate();
  const client = useQueryClient();
  return (
    <>
      <PageTitle
        eyebrow="PROJECTS"
        title="Projects"
        description="Independent repositories and behavior, backed by shared workers."
      />
      <Failure error={projects.error ?? error} />
      <Section title="Your projects">
        <div className="editor-body">
          {projects.isPending
            ? "Loading projects…"
            : projects.data?.map((p) => (
                <p key={p.id}>
                  <Link to={projectPath(p.id, "/repositories")}>{p.name}</Link>{" "}
                  · {p.slug}
                </p>
              ))}
        </div>
      </Section>
      <Section title="Create project">
        <form
          className="editor-body"
          onSubmit={async (e) => {
            e.preventDefault();
            setPending(true);
            setError("");
            try {
              const p = await api<Project>("/projects", { name, slug });
              await client.invalidateQueries({ queryKey: ["projects"] });
              navigate(projectPath(p.id, "/repositories"));
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setPending(false);
            }
          }}
        >
          <label>
            Project name
            <input
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Project slug
            <input
              required
              pattern="[a-z][a-z0-9-]*"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
            />
          </label>
          <button disabled={pending}>Create project</button>
        </form>
      </Section>
      <p>
        <Link to="/config">Default legacy profiles and saved history</Link>
      </p>
    </>
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
  const id = useProjectId();
  const project = useProjectData<Project>("");
  return (
    <div className="project-space">
      <nav className="tabs">
        {[
          ["repositories", "Repositories"],
          ["credentials", "Credentials"],
          ["config", "Project configuration"],
          ["pools", "Shared worker pools"],
        ].map(([path, label]) => (
          <NavLink key={path} to={projectPath(id, "/" + path)}>
            {label}
          </NavLink>
        ))}
        <Link to="/config">Legacy profiles</Link>
      </nav>
      <Failure error={project.error} />
      <Routes>
        <Route index element={<Navigate to="repositories" replace />} />
        <Route path="repositories" element={<Repositories />} />
        <Route path="credentials" element={<Credentials />} />
        <Route path="config" element={<ProjectConfiguration />} />
        <Route path="pools" element={<ProjectPools />} />
        <Route path="*" element={<p>Choose a project page above.</p>} />
      </Routes>
    </div>
  );
}
// A component may finish an already accepted write after navigation. Only its
// captured project's cache is invalidated; no late result repopulates a new form.
function useScopedWrite() {
  const id = useProjectId(),
    client = useQueryClient();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return {
    alive,
    write: async <T,>(path: string, body: unknown, method?: "PUT") => {
      const result = await api<T>(projectPath(id, path), body, { method });
      await client.invalidateQueries({ queryKey: ["project", id] });
      return result;
    },
  };
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
    [alias, setAlias] = useState(""),
    [owner, setOwner] = useState(""),
    [name, setName] = useState(""),
    [credential, setCredential] = useState(""),
    [overrides, setOverrides] = useState("{}"),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useScopedWrite();
  function edit(repo: Repository | null) {
    setSelected(repo);
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
        description="Aliases select a repository within this project. Running workflows retain their pinned revisions."
      />
      <Failure error={error || repositories.error || secrets.error} />
      <Section title="Connections">
        <div className="editor-body">
          {repositories.isPending
            ? "Loading repositories…"
            : repositories.data?.length
              ? repositories.data.map((r) => (
                  <p key={r.id}>
                    <button disabled={pending} onClick={() => edit(r)}>
                      {r.alias}
                    </button>{" "}
                    · {r.owner}/{r.name} <small>revision {r.revision_id}</small>
                  </p>
                ))
              : "No repositories connected."}
          <button disabled={pending} onClick={() => edit(null)}>
            New repository
          </button>
        </div>
      </Section>
      <Section title={selected ? "Edit connection" : "Connect repository"}>
        <form
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
                  owner,
                  name,
                  credential_id: credential || null,
                  overrides: object(overrides),
                  ...(selected ? { parent_id: selected.revision_id } : {}),
                },
                selected ? "PUT" : undefined,
              );
              if (alive.current) {
                edit(null);
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
            Repository alias
            <input
              required
              value={alias}
              onChange={(e) => setAlias(e.target.value)}
            />
          </label>
          <label>
            GitHub owner
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
            GitHub credential reference
            <select
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
            >
              <option value="">No credential</option>
              {secrets.data
                ?.filter(
                  (s) =>
                    s.purpose === "github" &&
                    !s.revoked_at &&
                    (!s.repository_id || s.repository_id === selected?.id),
                )
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} · {s.id}
                  </option>
                ))}
            </select>
          </label>
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
            . Repository identity and credentials are separate from behavior.
          </small>
          <button disabled={pending || repositories.isPending}>
            Save repository
          </button>
          {message && <p role="status">{message}</p>}
        </form>
      </Section>
      <EffectivePreview repositories={repositories.data ?? []} />
    </>
  );
}
function EffectivePreview({ repositories }: { repositories: Repository[] }) {
  const [repository, setRepository] = useState(""),
    [profile, setProfile] = useState("simple"),
    [preview, setPreview] = useState<unknown>(null),
    [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useScopedWrite();
  const selected = repositories.some((r) => r.alias === repository)
    ? repository
    : (repositories[0]?.alias ?? "");
  return (
    <Section title="Effective configuration preview">
      <form
        className="editor-body"
        onSubmit={async (e) => {
          e.preventDefault();
          setPending(true);
          setError("");
          setPreview(null);
          try {
            const result = await write("/config/effective", {
              repository: selected,
              profile,
            });
            if (alive.current) setPreview(result);
          } catch (e) {
            if (alive.current) setError((e as Error).message);
          } finally {
            if (alive.current) setPending(false);
          }
        }}
      >
        <label>
          Preview repository
          <select
            disabled={pending}
            value={selected}
            onChange={(e) => {
              setRepository(e.target.value);
              setPreview(null);
            }}
          >
            {repositories.map((r) => (
              <option key={r.id} value={r.alias}>
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
            }}
          />
        </label>
        <button disabled={!selected || pending}>
          Preview effective configuration
        </button>
        <Failure error={error} />
        {preview !== null && <pre>{JSON.stringify(preview, null, 2)}</pre>}
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
  return (
    <>
      <PageTitle
        eyebrow="PROJECT CREDENTIALS"
        title="Credential references"
        description="Values are encrypted by the server and never returned. References are scoped to this project or a repository."
      />
      <Failure error={error || secrets.error || repositories.error} />
      <Section title="Versions">
        <div className="editor-body">
          {secrets.data?.map((s) => (
            <p key={s.id}>
              {s.name} · {s.purpose} · {s.id} ·{" "}
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
            </p>
          ))}
        </div>
      </Section>
      <Section title="Create credential version">
        <form
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
              {["github", "openai", "anthropic"].map((p) => (
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
          <button disabled={pending}>Create credential</button>
        </form>
      </Section>
    </>
  );
}
export function ProjectConfiguration() {
  const revisions =
    useProjectData<{ id: string; summary: string }[]>("/config/revisions");
  const head = revisions.data?.[0]?.id;
  const revision = useProjectData<{ id: string; document: unknown }>(
    "/config/revisions/" + (head ?? ""),
    !!head,
  );
  const [source, setSource] = useState(""),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useScopedWrite();
  useEffect(() => {
    if (revision.data)
      setSource(JSON.stringify(revision.data.document, null, 2));
  }, [revision.data]);
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
            setPending(true);
            setError("");
            setMessage("");
            try {
              await write("/config/revisions", {
                parent_id: head,
                document: object(source),
                summary: "Project defaults updated from Control Pane",
              });
              if (alive.current) setMessage("Project configuration saved.");
            } catch (e) {
              if (alive.current) setError((e as Error).message);
            } finally {
              if (alive.current) setPending(false);
            }
          }}
        >
          <label>
            Project configuration JSON
            <textarea
              rows={20}
              value={source}
              onChange={(e) => setSource(e.target.value)}
            />
          </label>
          <button disabled={pending || !revision.data}>
            Save project configuration
          </button>
          {message && <p role="status">{message}</p>}
          <small>
            Active revision: {head ?? "unavailable"}. Provider grants are
            selected by workflow action bindings using the project credential
            IDs. Running actions keep their pinned settings.
          </small>
        </form>
      </Section>
      <Section title="Saved revisions">
        <div className="editor-body">
          {revisions.data?.map((r) => (
            <p key={r.id}>
              {r.id} · {r.summary}
            </p>
          ))}
        </div>
      </Section>
    </>
  );
}
interface Pool {
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
  }[];
}
function ProjectPools() {
  const pools = useProjectData<Pool[]>("/worker-pools");
  const { write, alive } = useScopedWrite();
  const [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  return (
    <>
      <PageTitle
        eyebrow="SHARED INFRASTRUCTURE"
        title="Shared worker pools"
        description="Pool capabilities and slots belong to deployment infrastructure. Membership authorizes this project; it does not select repository behavior."
      />
      <Failure error={error || pools.error} />
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
            <p>
              {p.role} · {p.normal_slots} normal / {p.durable_slots} durable
              slots per worker
            </p>
            <p>
              Executors: {p.executors.join(", ")}
              <br />
              Action versions: {p.actions.join(", ")}
            </p>
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
              p.workers.map((w) => (
                <div key={w.id}>
                  <strong>{w.name}</strong>
                  <p>
                    {w.capability_state} · {w.protocol} · desired{" "}
                    {w.desired_state}
                  </p>
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
                    Last heartbeat:{" "}
                    {w.last_heartbeat_at
                      ? new Date(w.last_heartbeat_at * 1000).toLocaleString()
                      : "unknown"}
                  </p>
                </div>
              ))
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
    <div>
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
