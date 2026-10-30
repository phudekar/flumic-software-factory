import { useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { AlertCircle, CheckCircle2, Circle } from "lucide-react";
import { Empty, Failure, PageTitle, Status } from "../components/ui";
import {
  humanWorkflowName,
  localTimestamp,
} from "../components/project-format";
import { projectPath, useProjectData, useProjectId } from "../project-context";
import { diagnostic, useWorkflowWrite } from "../workflow-api";
import type { Execution, PinnedStage, ProjectRun } from "../workflow-api";
import { RunEvidence } from "./project-run-evidence";
import "./project-run-inspector.css";

const views = [
  ["stages", "Stage explorer"],
  ["overview", "Overview"],
  ["activity", "Run activity"],
  ["models", "All model calls"],
] as const;
const tabs = [
  ["result", "Result"],
  ["logs", "Logs"],
  ["models", "Model calls"],
  ["config", "Configuration"],
] as const;

export function ProjectRunInspector({ id }: { id: string }) {
  const project = useProjectId();
  const run = useProjectData<ProjectRun>("/workflow-runs/" + id);
  const value = run.data;
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const requestedView =
    params.get("view") ??
    (location.hash === "#run-model-calls"
      ? "models"
      : location.hash === "#run-activity"
        ? "activity"
        : "stages");
  const view = views.some(([key]) => key === requestedView)
    ? requestedView
    : "stages";
  const requestedTab = params.get("detail");
  const tab = tabs.some(([key]) => key === requestedTab)
    ? requestedTab!
    : "result";
  const explicitStage = params.get("stage"),
    explicitAttempt = params.get("execution");
  const linkedAttempt = value?.attempts?.find((a) => a.id === explicitAttempt);
  const stageId =
    explicitStage ?? linkedAttempt?.stage_id ?? value?.stages?.[0]?.id;
  const selectedStage = value?.stages?.find((s) => s.id === stageId);
  const attempts =
    value?.attempts
      ?.filter((a) => a.stage_id === stageId)
      .sort((a, b) => b.generation - a.generation) ?? [];
  const attempt = explicitAttempt
    ? attempts.find((a) => a.id === explicitAttempt)
    : attempts[0];
  const invalid = Boolean(
    (explicitStage && !selectedStage) || (explicitAttempt && !attempt),
  );
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useWorkflowWrite();
  const navigate = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes))
      value === null ? next.delete(key) : next.set(key, value);
    setParams(next);
  };
  return (
    <>
      <PageTitle
        eyebrow="PROJECT EXECUTION"
        title={value ? humanWorkflowName(value.name) : "Workflow run"}
        description="Explore each stage’s result, captured activity and model calls."
        action={
          value && (
            <button
              className="run-cancel"
              disabled={
                pending ||
                ["completed", "failed", "cancelled", "cancelling"].includes(
                  value.state,
                )
              }
              onClick={async () => {
                setPending(true);
                setError("");
                try {
                  const receipt = await write<ProjectRun>(
                    "/workflow-runs/" + id + "/cancel",
                    {},
                  );
                  if (alive.current)
                    setMessage(
                      ["cancelled", "completed", "failed"].includes(
                        receipt.state,
                      )
                        ? "Cancellation receipt: " + receipt.state
                        : "Cancellation requested (receipt).",
                    );
                } catch (e) {
                  if (alive.current) setError(diagnostic(e));
                } finally {
                  if (alive.current) setPending(false);
                }
              }}
            >
              Request cancellation
            </button>
          )
        }
      />
      <Failure error={run.error || error} />
      {run.isPending && <p role="status">Loading run…</p>}
      {value && (
        <div className="run-inspector">
          <div className="project-run-summary">
            <div className="project-summary-tile">
              <span>Run status</span>
              <Status value={value.state} />
              <small>Execution state; business outcome is separate.</small>
            </div>
            <div className="project-summary-tile">
              <span>Created</span>
              <strong>
                <time>{localTimestamp(value.created_at)}</time>
              </strong>
              <small>Run accepted in your local timezone</small>
            </div>
            <div className="project-summary-tile">
              <span>Repository alias</span>
              <strong>{value.default_repository ?? "Not reported"}</strong>
              <small>Pinned for this run</small>
            </div>
            <div className="project-summary-tile">
              <span>Dispatch</span>
              <Status value={value.dispatch.state} />
              <small>{value.stages?.length ?? 0} declared stages</small>
            </div>
          </div>
          {["queued", "running", "cancelling"].includes(value.state) && (
            <p>Request accepted; execution is not complete.</p>
          )}
          <Failure error={value.dispatch.error} />
          {message && <p role="status">{message}</p>}
          {value.state === "cancelling" && (
            <p>Cancellation requested; execution may still be running.</p>
          )}
          <nav className="run-view-tabs" aria-label="Run views">
            {views.map(([key, title]) => (
              <button
                key={key}
                aria-pressed={view === key}
                onClick={() => navigate({ view: key })}
              >
                {title}
              </button>
            ))}
          </nav>
          {view === "overview" ? (
            <RunOverview value={value} />
          ) : view === "activity" || view === "models" ? (
            <RunEvidence
              key={id + view}
              runId={id}
              mode={view === "activity" ? "logs" : "models"}
            />
          ) : (
            <>
              {invalid && (
                <>
                  <Failure error="Unknown stage or execution for this run." />
                  <button
                    onClick={() => navigate({ stage: null, execution: null })}
                  >
                    Reset stage selection
                  </button>
                </>
              )}
              {!invalid &&
                (selectedStage ? (
                  <div className="run-stage-explorer" id="run-stages">
                    <nav
                      className="run-stage-rail"
                      aria-label="Select workflow stage"
                    >
                      <span className="run-rail-label">
                        WORKFLOW STAGES · {value.stages?.length}
                      </span>
                      {value.stages?.map((s, i) => {
                        const latest = value.attempts
                          ?.filter((a) => a.stage_id === s.id)
                          .sort((a, b) => b.generation - a.generation)[0];
                        const Icon =
                          latest?.state === "completed"
                            ? CheckCircle2
                            : latest?.state === "failed"
                              ? AlertCircle
                              : Circle;
                        return (
                          <button
                            key={s.id}
                            aria-pressed={s.id === stageId}
                            onClick={() =>
                              navigate({ stage: s.id, execution: null })
                            }
                          >
                            <Icon size={16} aria-hidden="true" />
                            <span>
                              <strong>
                                {i + 1}. {s.id.replaceAll("_", " ")}
                              </strong>
                              <small>
                                {latest
                                  ? latest.state.replaceAll("_", " ")
                                  : "No attempt recorded"}
                              </small>
                            </span>
                          </button>
                        );
                      })}
                      <small className="run-rail-note">
                        Declared stages; dependencies are shown in
                        Configuration.
                      </small>
                    </nav>
                    <section
                      className="run-stage-detail"
                      aria-label="Selected stage"
                    >
                      <div className="run-stage-header">
                        <div>
                          <span className="eyebrow">SELECTED STAGE</span>
                          <h2>{selectedStage.id.replaceAll("_", " ")}</h2>
                        </div>
                        <label className="run-attempt-select">
                          Stage attempt
                          <select
                            value={attempt?.id ?? ""}
                            disabled={!attempts.length}
                            onChange={(e) =>
                              navigate({ execution: e.target.value })
                            }
                          >
                            {attempts.length ? (
                              attempts.map((a) => (
                                <option key={a.id} value={a.id}>
                                  Attempt {a.generation} · {a.state}
                                </option>
                              ))
                            ) : (
                              <option value="">No attempt recorded</option>
                            )}
                          </select>
                        </label>
                      </div>
                      <div className="run-stage-meta">
                        {attempt && <Status value={attempt.state} />}
                        <span>
                          {selectedStage.action ??
                            selectedStage.image ??
                            selectedStage.executor}
                        </span>
                        {attempt?.worker_id && (
                          <Link
                            to={projectPath(
                              project,
                              "/workers/" + attempt.worker_id,
                            )}
                          >
                            Worker {attempt.worker_id}
                          </Link>
                        )}
                        {attempt?.job_id && (
                          <Link
                            to={
                              projectPath(project, "/jobs/" + attempt.job_id) +
                              "?" +
                              new URLSearchParams({ run: id })
                            }
                          >
                            Inspect container job
                          </Link>
                        )}
                      </div>
                      <nav
                        className="run-detail-tabs"
                        aria-label="Stage detail"
                      >
                        {tabs.map(([key, title]) => (
                          <button
                            key={key}
                            aria-pressed={tab === key}
                            onClick={() => navigate({ detail: key })}
                          >
                            {title}
                          </button>
                        ))}
                      </nav>
                      {tab === "config" ? (
                        <StageConfiguration
                          stage={selectedStage}
                          attempt={attempt}
                        />
                      ) : attempt ? (
                        <RunEvidence
                          key={[id, stageId, attempt.id, tab].join(":")}
                          runId={id}
                          stage={stageId}
                          execution={attempt.id}
                          mode={tab as "result" | "logs" | "models"}
                        />
                      ) : (
                        <Empty
                          title="No attempt recorded"
                          detail="This stage has no execution receipt yet. Run acceptance alone does not establish stage execution."
                        />
                      )}
                    </section>
                  </div>
                ) : (
                  <Empty
                    title="No stage snapshot available"
                    detail="No pinned stages are returned for this run."
                  />
                ))}
            </>
          )}
        </div>
      )}
    </>
  );
}

function StageConfiguration({
  stage,
  attempt,
}: {
  stage: PinnedStage;
  attempt?: Execution;
}) {
  return (
    <div className="run-stage-config">
      <h3>Pinned stage configuration</h3>
      <p>
        Recorded when this run was accepted. Current workflow edits do not
        change this snapshot.
      </p>
      <p>Depends on: {stage.after?.join(", ") || "start"}</p>
      <dl className="run-config-fields">
        <dt>Executor</dt>
        <dd>{stage.executor}</dd>
        <dt>Action / image</dt>
        <dd>{stage.action ?? stage.image ?? "Not reported"}</dd>
        <dt>Repository alias</dt>
        <dd>{stage.repository_alias}</dd>
        <dt>Worker pool</dt>
        <dd>{stage.pool_id}</dd>
        <dt>Timeout</dt>
        <dd>
          {stage.timeout_seconds == null
            ? "Unknown"
            : stage.timeout_seconds + " seconds"}
        </dd>
        <dt>Configured retries</dt>
        <dd>{stage.retries ?? "Unknown"}</dd>
        <dt>Inputs</dt>
        <dd>
          <pre>{JSON.stringify(stage.inputs ?? {}, null, 2)}</pre>
        </dd>
        <dt>Settings</dt>
        <dd>
          <pre>{JSON.stringify(stage.settings, null, 2)}</pre>
        </dd>
        {stage.when != null && (
          <>
            <dt>Condition</dt>
            <dd>
              <pre>{JSON.stringify(stage.when, null, 2)}</pre>
            </dd>
          </>
        )}
      </dl>
      <details className="setup-advanced">
        <summary>Raw configuration &amp; execution identifiers</summary>
        <pre>
          {JSON.stringify({ stage, execution: attempt ?? null }, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function RunOverview({ value }: { value: ProjectRun }) {
  const project = useProjectId();
  return (
    <div className="run-overview">
      <section className="run-overview-card">
        <h2>Execution summary</h2>
        <p>
          Run completion does not imply a pull request was created or approved.
          Inspect each stage’s recorded evidence for its outcome.
        </p>
        <Link
          to={projectPath(project, "/workflow-revisions/" + value.revision_id)}
        >
          Inspect pinned workflow revision
        </Link>
        <dl className="run-config-fields">
          <dt>Run</dt>
          <dd>{value.id}</dd>
          <dt>Snapshot</dt>
          <dd>{value.snapshot_id}</dd>
          <dt>Factory revision</dt>
          <dd>{value.factory_revision_id ?? "Not reported"}</dd>
          <dt>Engine run</dt>
          <dd>{value.dispatch.engine_id ?? "Not submitted"}</dd>
        </dl>
      </section>
      <section className="run-overview-card">
        <h2>Follow-up workflows</h2>
        <p>Separate runs accepted through configured follow-up bindings.</p>
        {value.followups?.length ? (
          value.followups.map((f) => (
            <article className="project-followup-card" key={f.id}>
              <div>
                <strong>{f.kind.replaceAll("_", " ")}</strong>
                <Status value={f.state} />
              </div>
              {f.result_run_id && (
                <Link to={projectPath(project, "/runs/" + f.result_run_id)}>
                  Inspect follow-up run
                </Link>
              )}
              <p>Dispatch: {f.dispatch?.state ?? "Not reported"}</p>
              <Failure error={f.error} />
              <details>
                <summary>Dispatch identifiers</summary>
                <pre>{JSON.stringify(f, null, 2)}</pre>
              </details>
            </article>
          ))
        ) : (
          <p>No follow-up receipts.</p>
        )}
      </section>
    </div>
  );
}
