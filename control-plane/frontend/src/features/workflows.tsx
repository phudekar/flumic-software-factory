import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { useData } from "../hooks";
import {
  PageTitle,
  Section,
  Empty,
  Failure,
  Status,
  date,
} from "../components/ui";
import { YamlEditor } from "../components/yaml-editor";

interface Definition {
  id: string;
  name: string;
  paused: boolean | null;
  pause_supported: boolean;
  cron_behavior?: string | null;
  scheduled_behavior?: string | null;
  input_example: Record<string, unknown>;
}
interface Receipt {
  id: string;
  workflow_name?: string;
  action?: string;
  status: string;
  created_at?: number;
  error?: string | null;
  result: { run_id?: string; deduplicated?: boolean };
}
type Verb = "trigger" | "pause" | "resume";
function inputMapping(value: Record<string, unknown>) {
  return value;
}

export function WorkflowsPage() {
  const workflows = useData<{ items: Definition[]; warnings: string[] }>(
    "workflows",
    "/workflows",
  );
  const [selection, setSelection] = useState<{
    workflow: Definition;
    action: Verb;
  } | null>(null);
  const [history, setHistory] = useState<Definition | null>(null);
  const [message, setMessage] = useState("");
  const [run, setRun] = useState<string | null>(null);
  const client = useQueryClient();
  function open(workflow: Definition, action: Verb) {
    setSelection({ workflow, action });
    setHistory(workflow);
    setMessage("");
    setRun(null);
  }
  return (
    <>
      <PageTitle
        eyebrow="WORKFLOW ORCHESTRATION"
        title="Workflows"
        description="Trigger, pause and resume factory workflows across all workers."
      />
      <Failure error={workflows.error} />
      {workflows.data?.warnings.map((warning) => (
        <div className="notice" key={warning}>
          {warning}
        </div>
      ))}
      {message && (
        <div className="notice" role="status">
          {message}{" "}
          {run && (
            <Link className="text-link" to={"/runs/" + run}>
              Inspect run →
            </Link>
          )}
        </div>
      )}
      <Section title="Factory workflow definitions">
        {!workflows.data?.items.length ? (
          <Empty
            title="No workflow definitions available"
            detail="Definitions appear after workers register with the workflow engine. Check engine connectivity when state is unavailable."
          />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Status</th>
                  <th>Paused trigger behavior</th>
                  <th>Controls</th>
                </tr>
              </thead>
              <tbody>
                {workflows.data.items.map((workflow) => (
                  <tr key={workflow.id}>
                    <td>
                      <strong>{workflow.name}</strong>
                      <small className="mono">{workflow.id.slice(0, 8)}</small>
                    </td>
                    <td>
                      <Status
                        value={
                          workflow.paused === null
                            ? "unknown"
                            : workflow.paused
                              ? "paused"
                              : "active"
                        }
                      />
                    </td>
                    <td>
                      {workflow.paused ? (
                        <>
                          Cron: {workflow.cron_behavior ?? "unknown"}
                          <small>
                            Scheduled:{" "}
                            {workflow.scheduled_behavior ?? "unknown"}
                          </small>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      <div className="actions">
                        <button
                          className="secondary"
                          onClick={() => open(workflow, "trigger")}
                        >
                          Trigger run
                        </button>
                        <button
                          className="secondary"
                          disabled={!workflow.pause_supported}
                          aria-label={
                            workflow.paused
                              ? "Resume workflow"
                              : "Pause workflow"
                          }
                          title={
                            !workflow.pause_supported
                              ? "Native pause support is unavailable or unknown"
                              : undefined
                          }
                          onClick={() =>
                            open(workflow, workflow.paused ? "resume" : "pause")
                          }
                        >
                          {workflow.paused ? "Resume" : "Pause"}
                        </button>
                        <button
                          className="secondary"
                          onClick={() => setHistory(workflow)}
                        >
                          Action history
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <div className="notice">
        Runs require a compatible running worker.{" "}
        <Link className="text-link" to="/workers">
          Manage workers →
        </Link>
      </div>
      <div className="notice">
        Pause prevents new runs from starting. In-flight work continues. Manual
        and event triggers queue while paused; cron and scheduled triggers
        follow the selected skip/queue behavior. Resume may release queued work.
      </div>
      {history && <ActionHistory workflow={history} />}
      {selection && (
        <ControlDialog
          key={selection.workflow.id + selection.action}
          {...selection}
          onClose={() => setSelection(null)}
          onComplete={async (receipt) => {
            setSelection(null);
            setMessage(
              selection.action === "trigger"
                ? receipt.result.deduplicated
                  ? "A matching workflow run is already active."
                  : "Workflow run submitted."
                : selection.action === "pause"
                  ? "Workflow paused."
                  : "Workflow resumed.",
            );
            setRun(receipt.result.run_id ?? null);
            await Promise.all([
              client.invalidateQueries({ queryKey: ["workflows"] }),
              client.invalidateQueries({ queryKey: ["workflow-actions"] }),
              client.invalidateQueries({ queryKey: ["runs"] }),
            ]);
          }}
        />
      )}
    </>
  );
}

function ActionHistory({ workflow }: { workflow: Definition }) {
  const actions = useData<Receipt[]>(
    "workflow-actions",
    "/workflows/" + workflow.id + "/actions",
  );
  return (
    <Section title={"Action history · " + workflow.name}>
      <Failure error={actions.error} />
      {!actions.data?.length ? (
        <p className="muted">No control actions recorded.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Requested</th>
                <th>Action</th>
                <th>Request outcome</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {actions.data.map((operation) => (
                <tr key={operation.id}>
                  <td>{date(operation.created_at)}</td>
                  <td>{operation.action}</td>
                  <td>
                    <Status value={operation.status} />
                  </td>
                  <td>
                    {operation.result.run_id && (
                      <Link
                        className="text-link"
                        to={"/runs/" + operation.result.run_id}
                      >
                        Inspect run →
                      </Link>
                    )}
                    {operation.error && (
                      <small className="error-text">{operation.error}</small>
                    )}
                    <small className="mono">{operation.id}</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function ControlDialog({
  workflow,
  action,
  onClose,
  onComplete,
}: {
  workflow: Definition;
  action: Verb;
  onClose: () => void;
  onComplete: (result: Receipt) => Promise<void>;
}) {
  const [input, setInput] = useState(workflow.input_example);
  const [inputError, setInputError] = useState<string | null>(null);
  const [hours, setHours] = useState(24);
  const [cron, setCron] = useState("DROP");
  const [scheduled, setScheduled] = useState("DROP");
  const [key] = useState(() => crypto.randomUUID());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState<Record<string, unknown> | null>(
    null,
  );
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const client = useQueryClient();
  async function submit() {
    setPending(true);
    setError("");
    const body = submitted ?? {
      action,
      input: action === "trigger" ? input : {},
      queue_ttl_seconds: hours * 3600,
      cron_behavior: cron,
      scheduled_behavior: scheduled,
    };
    setSubmitted(body);
    try {
      const result = await api<Receipt>(
        "/workflows/" + workflow.id + "/actions",
        body,
        { idempotencyKey: key },
      );
      setReceipt(result);
      await client.invalidateQueries({ queryKey: ["workflow-actions"] });
      if (result.status === "completed") await onComplete(result);
    } catch (error) {
      setError((error as Error).message);
      if (error instanceof ApiError && error.status < 500) setSubmitted(null);
    } finally {
      setPending(false);
    }
  }
  return (
    <div className="modal-backdrop">
      <form
        className="modal wide"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="panel-heading">
          <h2>
            {action === "trigger"
              ? "Trigger workflow run"
              : action === "pause"
                ? "Pause workflow"
                : "Resume workflow"}
          </h2>
          <button type="button" className="secondary" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="mono">{workflow.name}</p>
        {action === "trigger" && (
          <>
            <p>
              Creates a new run. Runs can invoke models and update GitHub.
              Replace null placeholders with the intended issue or pull request
              details.
            </p>
            {workflow.paused && (
              <div className="notice">
                This workflow is paused. The submitted run will queue until
                resumed or its queue TTL expires.
              </div>
            )}
            <YamlEditor
              value={input}
              label="Workflow run input YAML"
              readOnly={!!submitted}
              onChange={setInput}
              onError={setInputError}
              normalize={inputMapping}
              maxBytes={65536}
            />
          </>
        )}
        {action === "pause" && (
          <>
            <p>
              Active runs continue. Choose what future cron and scheduled
              triggers do while paused.
            </p>
            <label>
              Cron triggers while paused
              <select
                value={cron}
                disabled={!!submitted}
                onChange={(event) => setCron(event.target.value)}
              >
                <option value="DROP">Skip missed triggers</option>
                <option value="QUEUE">Queue for resume</option>
              </select>
            </label>
            <label>
              Scheduled triggers while paused
              <select
                value={scheduled}
                disabled={!!submitted}
                onChange={(event) => setScheduled(event.target.value)}
              >
                <option value="DROP">Skip missed triggers</option>
                <option value="QUEUE">Queue for resume</option>
              </select>
            </label>
            <label>
              Queue TTL (hours)
              <input
                type="number"
                min={1}
                max={720}
                step={1}
                required
                value={hours}
                readOnly={!!submitted}
                onChange={(event) => setHours(Number(event.target.value))}
              />
            </label>
            <small>
              Manual and event-triggered runs queue regardless of these choices.
              Expired queued runs are cancelled by Hatchet.
            </small>
          </>
        )}
        {action === "resume" && (
          <p>
            Allow new runs to start and release queued runs that have not
            expired.
          </p>
        )}
        <Failure error={error || receipt?.error} />
        {submitted && !receipt && error && (
          <p>
            The request may still be processing. Retry uses the same action key
            and input.
          </p>
        )}
        {receipt && receipt.status !== "completed" && (
          <div className="notice">
            Action status: {receipt.status}. Close this dialog and inspect
            action history and{" "}
            <Link className="text-link" to="/runs">
              workflow runs
            </Link>{" "}
            before issuing another action.
          </div>
        )}
        <button
          className="primary"
          disabled={pending || !!inputError || !!receipt}
        >
          {pending
            ? "Submitting…"
            : submitted
              ? "Retry request"
              : "Confirm " + action}
        </button>
      </form>
    </div>
  );
}
