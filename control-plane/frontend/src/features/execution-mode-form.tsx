import { useState } from "react";
import { Link } from "react-router-dom";
import { Shield, KeyRound } from "lucide-react";
import { ApiError } from "../api";
import { Failure, Section } from "../components/ui";
import {
  projectPath,
  useProjectData,
  useProjectId,
  useScopedWrite,
} from "../project-context";
import type { ExecutionMode, ExecutionPolicy } from "../workflow-api";
import "./execution-modes.css";

export type ModeSelection = "project" | ExecutionMode;
const modes: readonly string[] = ["direct", "protected"];

export function isExecutionPolicy(value: unknown): value is ExecutionPolicy {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const policy = value as ExecutionPolicy;
  return (
    typeof policy.revision_id === "string" &&
    policy.revision_id.length > 0 &&
    modes.includes(policy.default_mode) &&
    modes.includes(policy.minimum_mode) &&
    Number.isSafeInteger(policy.admitted_direct_runs) &&
    policy.admitted_direct_runs >= 0 &&
    typeof policy.protected_execution_available === "boolean" &&
    !(policy.minimum_mode === "protected" && policy.default_mode === "direct")
  );
}

export function executionModeBlocker(
  policy: ExecutionPolicy | undefined,
  selection: ModeSelection,
): string {
  if (!isExecutionPolicy(policy))
    return "Project execution policy must be loaded before launching.";
  const resolved = selection === "project" ? policy.default_mode : selection;
  if (!modes.includes(resolved)) return "Choose a supported execution mode.";
  if (resolved === "direct" && policy.minimum_mode === "protected")
    return "The project minimum requires protected execution.";
  if (resolved === "protected" && !policy.protected_execution_available)
    return "Protected execution is not ready for this project. Qualified broker and sandbox support is required.";
  return "";
}

export function ExecutionModeBadge({ mode }: { mode: ExecutionMode }) {
  const Icon = mode === "protected" ? Shield : KeyRound;
  return (
    <span className={`execution-mode-badge ${mode}`} title="Execution mode">
      <Icon size={14} aria-hidden="true" />
      {mode === "protected" ? "Protected" : "Direct"}
    </span>
  );
}

export function ExecutionModeChoice({
  policy,
  value,
  onChange,
  disabled = false,
  loading = false,
}: {
  policy?: ExecutionPolicy;
  value: ModeSelection;
  onChange: (mode: ModeSelection) => void;
  disabled?: boolean;
  loading?: boolean;
}) {
  const valid = isExecutionPolicy(policy),
    blocker = executionModeBlocker(valid ? policy : undefined, value);
  return (
    <div className="execution-mode-choice">
      <label>
        Run execution mode
        <select
          disabled={disabled || !valid}
          value={value}
          onChange={(event) => onChange(event.target.value as ModeSelection)}
        >
          <option value="project">
            Use project default{valid ? ` (${policy.default_mode})` : ""}
          </option>
          <option
            value="direct"
            disabled={policy?.minimum_mode === "protected"}
          >
            Direct
          </option>
          <option
            value="protected"
            disabled={!policy?.protected_execution_available}
          >
            Protected
            {policy?.protected_execution_available ? "" : " — unavailable"}
          </option>
        </select>
      </label>
      {blocker ? (
        <p className="notice" role="status">
          {loading ? "Checking project execution policy…" : blocker}
        </p>
      ) : (
        <small>
          {(value === "project" ? policy?.default_mode : value) === "direct"
            ? "Provider credentials are available to trusted worker processes."
            : "Uses the project's approved protected execution configuration."}{" "}
          The server revalidates policy when accepting the run.
        </small>
      )}
    </div>
  );
}

export function ExecutionModeForm() {
  const project = useProjectId();
  return <ExecutionModeEditor key={project} project={project} />;
}

function ExecutionModeEditor({ project }: { project: string }) {
  const query = useProjectData<ExecutionPolicy>("/execution-policy"),
    { write, alive } = useScopedWrite();
  const [draft, setDraft] = useState<ExecutionPolicy | null>(null);
  const [pending, setPending] = useState(false),
    [error, setError] = useState("");
  const [message, setMessage] = useState(""),
    [conflict, setConflict] = useState(false);
  const [ackBlocking, setAckBlocking] = useState(false),
    [ackWeakening, setAckWeakening] = useState(false);
  const current = isExecutionPolicy(query.data) ? query.data : undefined;
  if (!current)
    return (
      <Section title="Execution policy">
        <div className="editor-body">
          {query.isPending ? (
            <p role="status">Loading execution policy…</p>
          ) : (
            <Failure
              error={
                query.error ||
                "The server returned an unrecognized execution policy. Reload before changing execution modes."
              }
            />
          )}
        </div>
      </Section>
    );
  const stale =
    conflict || (!!draft && current.revision_id !== draft.revision_id);
  const blocking =
    !!draft &&
    draft.default_mode === "protected" &&
    !current.protected_execution_available;
  const weakening =
    !!draft &&
    current.minimum_mode === "protected" &&
    draft.minimum_mode === "direct";
  const unchanged =
    !!draft &&
    draft.default_mode === current.default_mode &&
    draft.minimum_mode === current.minimum_mode;
  const update = (
    field: "default_mode" | "minimum_mode",
    value: ExecutionMode,
  ) => {
    setDraft(
      (old) =>
        old && {
          ...old,
          [field]: value,
          ...(field === "minimum_mode" && value === "protected"
            ? { default_mode: "protected" as const }
            : {}),
        },
    );
    setAckBlocking(false);
    setAckWeakening(false);
    setError("");
  };
  return (
    <Section title="Execution policy">
      <div className="editor-body execution-policy">
        <p>
          Choose how new runs execute. The project minimum prevents weaker
          launch overrides.
        </p>
        <div className="execution-mode-options">
          <div>
            <KeyRound size={20} aria-hidden="true" />
            <strong>Direct</strong>
            <p>
              Supports current SDK and CLI integrations. Provider credentials
              are available in trusted worker processes.
            </p>
          </div>
          <div>
            <Shield size={20} aria-hidden="true" />
            <strong>Protected</strong>
            <p>
              Requires brokered access and a qualified isolated runtime.
              {current.protected_execution_available
                ? ""
                : " Not available in this deployment."}
            </p>
          </div>
        </div>
        {!current.protected_execution_available && (
          <p className="notice">
            Protected execution is not ready for this project. A saved policy
            does not install or qualify a runtime.
          </p>
        )}
        <p>
          {current.admitted_direct_runs.toLocaleString()} admitted direct runs
          keep their current authorization. Saving policy does not cancel them.
        </p>
        <Failure error={error || query.error} />
        {stale && (
          <p className="notice" role="status">
            The saved policy changed. Your edits are retained. Cancel and reload
            before saving again.
          </p>
        )}
        {message && (
          <p className="notice success" role="status">
            {message}
          </p>
        )}
        {draft ? (
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              if (
                pending ||
                stale ||
                unchanged ||
                (blocking && !ackBlocking) ||
                (weakening && !ackWeakening)
              )
                return;
              setPending(true);
              setError("");
              setMessage("");
              try {
                await write<ExecutionPolicy>(
                  "/execution-policy",
                  {
                    expected_revision: draft.revision_id,
                    default_mode: draft.default_mode,
                    minimum_mode: draft.minimum_mode,
                  },
                  "PUT",
                );
                if (alive.current) {
                  setDraft(null);
                  setMessage(
                    "Execution policy saved. Existing admitted runs keep their pinned mode.",
                  );
                }
              } catch (failure) {
                if (alive.current) {
                  setError(
                    failure instanceof Error
                      ? failure.message
                      : "Execution policy could not be saved.",
                  );
                  if (failure instanceof ApiError && failure.status === 409) {
                    setConflict(true);
                    await query.refetch();
                  }
                }
              } finally {
                if (alive.current) setPending(false);
              }
            }}
          >
            <div className="execution-mode-fields">
              <label>
                Default execution mode
                <select
                  disabled={pending}
                  value={draft.default_mode}
                  onChange={(event) =>
                    update("default_mode", event.target.value as ExecutionMode)
                  }
                >
                  <option
                    value="direct"
                    disabled={draft.minimum_mode === "protected"}
                  >
                    Direct
                  </option>
                  <option value="protected">
                    Protected
                    {current.protected_execution_available
                      ? ""
                      : " — unavailable until qualified"}
                  </option>
                </select>
              </label>
              <label>
                Minimum execution mode
                <select
                  disabled={pending}
                  value={draft.minimum_mode}
                  onChange={(event) =>
                    update("minimum_mode", event.target.value as ExecutionMode)
                  }
                >
                  <option value="direct">Allow direct execution</option>
                  <option value="protected">Require protected execution</option>
                </select>
              </label>
            </div>
            {blocking && (
              <label className="execution-mode-consent">
                <input
                  type="checkbox"
                  checked={ackBlocking}
                  disabled={pending}
                  onChange={(event) => setAckBlocking(event.target.checked)}
                />
                {draft.minimum_mode === "protected"
                  ? "I understand that requiring protected execution blocks all new runs until qualified support is ready."
                  : "I understand that launches using this protected default will be blocked. Explicit direct overrides remain allowed."}
              </label>
            )}
            {weakening && (
              <label className="execution-mode-consent">
                <input
                  type="checkbox"
                  checked={ackWeakening}
                  disabled={pending}
                  onChange={(event) => setAckWeakening(event.target.checked)}
                />
                Allow future runs to use direct execution.
              </label>
            )}
            <div className="execution-mode-actions">
              <button
                className="primary"
                disabled={
                  pending ||
                  stale ||
                  unchanged ||
                  (blocking && !ackBlocking) ||
                  (weakening && !ackWeakening)
                }
              >
                {pending ? "Saving policy…" : "Save execution policy"}
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => {
                  setDraft(null);
                  setConflict(false);
                  setError("");
                  setAckBlocking(false);
                  setAckWeakening(false);
                }}
              >
                {stale ? "Cancel and reload" : "Cancel edit"}
              </button>
            </div>
          </form>
        ) : (
          <div className="execution-mode-actions">
            <span>
              Default: <ExecutionModeBadge mode={current.default_mode} />
            </span>
            <span>
              Minimum:{" "}
              {current.minimum_mode === "protected"
                ? "Protected required"
                : "Direct allowed"}
            </span>
            <button
              type="button"
              onClick={() => {
                setDraft({ ...current });
                setMessage("");
                setError("");
                setAckBlocking(false);
                setAckWeakening(false);
              }}
            >
              Edit execution policy
            </button>
          </div>
        )}
        <details>
          <summary>Policy details</summary>
          <p>
            Revision: <code>{current.revision_id}</code>. Only a deployment
            administrator can change this policy. To stop existing work, inspect
            and cancel each run explicitly.
          </p>
        </details>
        <Link to={projectPath(project, "/runs")}>Review workflow runs</Link>
      </div>
    </Section>
  );
}
