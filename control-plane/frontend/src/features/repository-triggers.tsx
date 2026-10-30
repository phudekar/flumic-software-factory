import { useSetupForm } from "../hooks";
import { humanWorkflowName } from "../components/project-format";
import "./project-editors.css";
import { useState } from "react";
import { type RepositoryConnection, type RepositoryTrigger } from "../api";
import { useProjectData, useScopedWrite } from "../project-context";
import { Failure, PageTitle, Section, Status } from "../components/ui";

function object(source: string) {
  const parsed = JSON.parse(source);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
    throw new Error("Use a JSON object.");
  return parsed;
}
type TokenResponse = RepositoryTrigger & { webhook_token?: string };
export function RepositoryTriggersPage() {
  const repositories = useProjectData<RepositoryConnection[]>("/repositories");
  const definitions = useProjectData<{ id: string; name: string }[]>(
    "/workflow-definitions",
  );
  const triggers = useProjectData<RepositoryTrigger[]>("/repository-triggers");
  const { write, alive } = useScopedWrite();
  const [formOpen, setFormOpen] = useState(false);
  const formRef = useSetupForm(formOpen);
  const [selected, setSelected] = useState<RepositoryTrigger | null>(null);
  const [repository, setRepository] = useState("");
  const [definition, setDefinition] = useState("");
  const [kind, setKind] =
    useState<RepositoryTrigger["event_kind"]>("issue-ready");
  const [enabled, setEnabled] = useState(true),
    [ready, setReady] = useState(true);
  const [defaults, setDefaults] = useState("{}"),
    [followups, setFollowups] = useState("{}");
  const [token, setToken] = useState(""),
    [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  function edit(item: RepositoryTrigger | null) {
    setFormOpen(true);
    setSelected(item);
    setRepository(item?.repository_id ?? "");
    setDefinition(item?.definition_id ?? "");
    setKind(item?.event_kind ?? "issue-ready");
    setEnabled(item?.enabled ?? true);
    setReady(item?.configuration.require_ready_label ?? true);
    setDefaults(
      JSON.stringify(item?.configuration.input_defaults ?? {}, null, 2),
    );
    setFollowups(JSON.stringify(item?.configuration.followups ?? {}, null, 2));
    setToken("");
    setError("");
  }
  async function changeToken(action: "rotate-token" | "revoke-token") {
    if (!selected) return;
    setPending(true);
    setError("");
    setToken("");
    try {
      const response = await write<TokenResponse>(
        "/repository-triggers/" + selected.id + "/" + action,
        { parent_id: selected.token_version_id },
      );
      if (alive.current) {
        const { webhook_token, ...safe } = response;
        setSelected(safe);
        setToken(webhook_token ?? "");
      }
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setPending(false);
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="PROJECT AUTOMATION"
        title="Repository triggers"
        description="Bind GitLab source events to owned workflow definitions. Accepted runs retain their pinned revisions; polling recovers missing deliveries."
      />
      <Failure
        error={
          error || repositories.error || definitions.error || triggers.error
        }
      />
      <Section
        title="Bindings"
        aside={
          <button
            className="primary"
            disabled={pending || formOpen}
            onClick={() => edit(null)}
          >
            New trigger
          </button>
        }
      >
        {triggers.isPending ? (
          <p className="editor-body">Loading bindings…</p>
        ) : triggers.data?.length ? (
          <div className="table-scroll">
            <table className="trigger-table">
              <thead>
                <tr>
                  <th>Repository</th>
                  <th>Event</th>
                  <th>Workflow</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {triggers.data.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <strong>
                        {repositories.data?.find(
                          (repo) => repo.id === item.repository_id,
                        )?.alias ?? "Repository unavailable"}
                      </strong>
                    </td>
                    <td>
                      {item.event_kind === "issue-ready"
                        ? "Ready issue"
                        : item.event_kind === "feedback"
                          ? "Merge request feedback"
                          : "Merged change verification"}
                    </td>
                    <td>
                      {humanWorkflowName(
                        definitions.data?.find(
                          (d) => d.id === item.definition_id,
                        )?.name ?? "Workflow unavailable",
                      )}
                    </td>
                    <td>
                      <Status value={item.enabled ? "enabled" : "disabled"} />
                      {item.last_error && <Failure error={item.last_error} />}
                    </td>
                    <td>
                      <button
                        aria-label={"Edit " + item.id}
                        disabled={pending}
                        onClick={() => edit(item)}
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !triggers.error && (
            <div className="empty">
              <h3>No automatic triggers configured</h3>
              <p>
                Runs can still be launched from a saved workflow. Add a binding
                to accept GitLab events automatically after repository
                validation and workflow setup.
              </p>
            </div>
          )
        )}
      </Section>
      {formOpen && (
        <>
          <Section title={selected ? "Edit binding" : "Create binding"}>
            <form
              className="editor-body trigger-form"
              ref={formRef}
              onSubmit={async (e) => {
                e.preventDefault();
                setPending(true);
                setError("");
                setToken("");
                try {
                  const response = await write<TokenResponse>(
                    "/repository-triggers" +
                      (selected ? "/" + selected.id : ""),
                    {
                      ...(selected
                        ? { parent_id: selected.revision_id }
                        : { repository_id: repository, event_kind: kind }),
                      definition_id: definition,
                      enabled,
                      configuration: {
                        require_ready_label: ready,
                        input_defaults: object(defaults),
                        followups: object(followups),
                      },
                    },
                    selected ? "PUT" : undefined,
                  );
                  if (alive.current) {
                    const { webhook_token, ...safe } = response;
                    setSelected(safe);
                    setToken(webhook_token ?? "");
                  }
                } catch (e) {
                  if (alive.current) setError((e as Error).message);
                } finally {
                  if (alive.current) setPending(false);
                }
              }}
            >
              <fieldset>
                <legend>Source and workflow</legend>
                <label>
                  Trigger repository
                  <select
                    required
                    disabled={!!selected || pending}
                    value={repository}
                    onChange={(e) => setRepository(e.target.value)}
                  >
                    <option value="">Choose validated GitLab repository</option>
                    {repositories.data
                      ?.filter(
                        (repo) => repo.provider === "gitlab" && repo.validated,
                      )
                      .map((repo) => (
                        <option key={repo.id} value={repo.id}>
                          {repo.alias}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  Event kind
                  <select
                    disabled={!!selected || pending}
                    value={kind}
                    onChange={(e) =>
                      setKind(e.target.value as RepositoryTrigger["event_kind"])
                    }
                  >
                    <option value="issue-ready">Ready issue</option>
                    <option value="feedback">Merge request feedback</option>
                    <option value="verify">Merged change verification</option>
                  </select>
                </label>
                <label>
                  Workflow definition
                  <select
                    required
                    disabled={pending}
                    value={definition}
                    onChange={(e) => setDefinition(e.target.value)}
                  >
                    <option value="">Choose workflow</option>
                    {definitions.data?.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </label>
                <small>
                  The workflow default repository must match this binding. New
                  acceptance freezes the current workflow and repository
                  revisions.
                </small>
              </fieldset>
              <fieldset>
                <legend>Execution rules</legend>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(e) => setEnabled(e.target.checked)}
                  />
                  Enabled
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={ready}
                    onChange={(e) => setReady(e.target.checked)}
                  />
                  Require ready label for issue triggers
                </label>
              </fieldset>
              <details className="setup-advanced">
                <summary>Advanced inputs and follow-up bindings</summary>
                <label>
                  Default workflow inputs (JSON)
                  <textarea
                    rows={4}
                    value={defaults}
                    onChange={(e) => setDefaults(e.target.value)}
                  />
                </label>
                <label>
                  Follow-up workflow definitions (JSON)
                  <textarea
                    rows={5}
                    value={followups}
                    onChange={(e) => setFollowups(e.target.value)}
                  />
                </label>
                <small>
                  Use repository UUID → issue/feedback/verify definition UUIDs.
                  All three roles are required for each supplied repository;
                  builtin workflows require follow-up bindings. Keep credentials
                  in project secrets.
                </small>
              </details>
              <div className="trigger-actions">
                <button
                  className="primary"
                  disabled={
                    pending || repositories.isPending || definitions.isPending
                  }
                >
                  Save trigger
                </button>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => {
                    setFormOpen(false);
                    setSelected(null);
                    setToken("");
                    setError("");
                  }}
                >
                  Cancel
                </button>
              </div>
            </form>
          </Section>
          {selected && (
            <Section title="Webhook credentials">
              <div className="editor-body">
                <p>
                  Webhook URL: <code>{selected.webhook_url}</code>
                </p>
                <p>Token version: {selected.token_version_id ?? "revoked"}</p>
                <button
                  disabled={pending}
                  onClick={() => void changeToken("rotate-token")}
                >
                  Rotate webhook token
                </button>
                <button
                  disabled={pending || !selected.token_version_id}
                  onClick={() => void changeToken("revoke-token")}
                >
                  Revoke webhook token
                </button>
                <p>
                  Use the token as GitLab's Secret token over HTTPS. Rotation
                  immediately invalidates the previous token. Revoking stops
                  webhooks; disable this binding to stop polling too.
                </p>
              </div>
            </Section>
          )}
        </>
      )}
      {token && (
        <Section title="Save this webhook token">
          <div className="editor-body">
            <p>
              This value is shown once. Save it in GitLab before dismissing.
            </p>
            <output aria-label="Webhook token">{token}</output>
            <p>
              <button onClick={() => setToken("")}>Dismiss token</button>
            </p>
          </div>
        </Section>
      )}
    </>
  );
}
