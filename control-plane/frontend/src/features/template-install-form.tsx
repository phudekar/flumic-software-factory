import { useState } from "react";
import { Link } from "react-router-dom";
import { useSetupForm } from "../hooks";
import { Failure, Section } from "../components/ui";
import {
  projectPath,
  useProjectData,
  useProjectId,
  useScopedWrite,
} from "../project-context";
interface Connection {
  id: string;
  alias: string;
}
interface Pool {
  id: string;
  name: string;
  role: string;
  authorized: boolean;
  actions: string[];
  executors: string[];
}
interface Secret {
  id: string;
  name: string;
  purpose: string;
  repository_id: string | null;
  revoked_at: number | null;
}
const requiredActions = {
  simple: [
    "issue.load@1",
    "issue.plan@1",
    "issue.triage@1",
    "change.review@1",
    "workflow.record@1",
    "pull.verify@1",
  ],
  developer: ["change.implement@1", "pull.feedback@1"],
};
export function TemplateInstallForm({ close }: { close: () => void }) {
  const project = useProjectId(),
    { write, alive } = useScopedWrite();
  const repositories = useProjectData<Connection[]>("/repositories"),
    pools = useProjectData<Pool[]>("/worker-pools"),
    secrets = useProjectData<Secret[]>("/secrets");
  const [repository, setRepository] = useState(""),
    [simple, setSimple] = useState(""),
    [developer, setDeveloper] = useState("");
  const [openai, setOpenai] = useState(""),
    [anthropic, setAnthropic] = useState("");
  const [error, setError] = useState(""),
    [pending, setPending] = useState(false),
    [receipt, setReceipt] = useState<Record<string, string> | null>(null);
  const formRef = useSetupForm(true);
  const connection = repositories.data?.find((r) => r.alias === repository);
  const eligible = (role: "simple" | "developer") =>
    (pools.data ?? []).filter(
      (p) =>
        p.authorized &&
        p.role === role &&
        p.executors.includes("builtin@1") &&
        requiredActions[role].every((a) => p.actions.includes(a)),
    );
  const credentials = (purpose: string) =>
    (secrets.data ?? []).filter(
      (s) =>
        s.revoked_at == null &&
        s.purpose === purpose &&
        (!s.repository_id || s.repository_id === connection?.id),
    );
  const valid =
    !!connection &&
    eligible("simple").some((p) => p.id === simple) &&
    eligible("developer").some((p) => p.id === developer) &&
    (!openai || credentials("openai").some((s) => s.id === openai)) &&
    (!anthropic || credentials("anthropic").some((s) => s.id === anthropic));
  return (
    <Section title="Install standard workflow templates">
      <Failure
        error={error || repositories.error || pools.error || secrets.error}
      />
      {receipt ? (
        <div className="editor-body" role="status">
          <p>
            Template revisions are ready. Existing recipes were retained;
            changed selections do not update their saved pools or credential
            grants. Inspect each revision before running.
          </p>
          {Object.entries(receipt).map(([kind, id]) => (
            <p key={kind}>
              <Link to={projectPath(project, "/workflow-revisions/" + id)}>
                {kind}: view saved revision
              </Link>
            </p>
          ))}
          <button onClick={close}>Done</button>
        </div>
      ) : (
        <form
          ref={formRef}
          className="editor-body"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!valid || pending) return;
            setPending(true);
            setError("");
            try {
              const result = await write<Record<string, string>>(
                "/workflow-definitions/seed-builtins",
                {
                  repository,
                  simple_pool: simple,
                  developer_pool: developer,
                  secrets: {
                    ...(openai ? { openai } : {}),
                    ...(anthropic ? { anthropic } : {}),
                  },
                },
              );
              if (alive.current) setReceipt(result);
            } catch (e) {
              if (alive.current) setError((e as Error).message);
            } finally {
              if (alive.current) setPending(false);
            }
          }}
        >
          <p>
            Creates issue-to-PR, feedback and verification recipes. Existing
            recipes are never overwritten. Installation does not hand over a
            repository or launch work.
          </p>
          <fieldset className="pool-form-fields" disabled={pending}>
            <label>
              Target repository
              <select
                required
                value={repository}
                onChange={(e) => {
                  setRepository(e.target.value);
                  setOpenai("");
                  setAnthropic("");
                }}
              >
                <option value="">Choose repository</option>
                {repositories.data?.map((r) => (
                  <option key={r.id} value={r.alias}>
                    {r.alias}
                  </option>
                ))}
              </select>
            </label>
            {(["simple", "developer"] as const).map((role) => (
              <label key={role}>
                {role === "simple"
                  ? "Simple worker pool"
                  : "Developer worker pool"}
                <select
                  required
                  value={role === "simple" ? simple : developer}
                  onChange={(e) =>
                    (role === "simple" ? setSimple : setDeveloper)(
                      e.target.value,
                    )
                  }
                >
                  <option value="">Choose authorized compatible pool</option>
                  {eligible(role).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <p>
              Only project-authorized pools advertising the required built-in
              actions are listed. Capacity and worker registration must still be
              checked.
            </p>
            {(["openai", "anthropic"] as const).map((purpose) => (
              <label key={purpose}>
                {purpose === "openai"
                  ? "OpenAI credential grant"
                  : "Anthropic credential grant"}
                <select
                  disabled={!connection}
                  value={purpose === "openai" ? openai : anthropic}
                  onChange={(e) =>
                    (purpose === "openai" ? setOpenai : setAnthropic)(
                      e.target.value,
                    )
                  }
                >
                  <option value="">No grant</option>
                  {credentials(purpose).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <p>
              Select every provider required by your saved model/backend
              configuration. These are encrypted credential references;
              GitHub/GitLab access comes from the repository connection.
            </p>
            <button className="primary" disabled={!valid || pending}>
              Install templates
            </button>
            <button type="button" onClick={close}>
              Cancel
            </button>
          </fieldset>
        </form>
      )}
    </Section>
  );
}
