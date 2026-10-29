import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import type { FactoryEvent, Workers } from "../api";
import { date, Failure, PageTitle, Section, Status } from "../components/ui";
import { useData } from "../hooks";
import { projectPath, useProjectData, useProjectId } from "../project-context";
import { diagnostic, useWorkflowWrite } from "../workflow-api";
import type {
  ProjectRun,
  ProjectJob,
  ModelAccountingEvent,
} from "../workflow-api";
import { ProjectAccounting } from "./usage";
import { ProjectActivity } from "../components/project-activity";
import { ProjectRunTable } from "../components/project-run-table";
import {
  humanWorkflowName,
  localTimestamp,
} from "../components/project-format";
import { ProjectModelCalls } from "./project-model-calls";
export function ProjectRunsPage() {
  const runs = useProjectData<ProjectRun[]>("/workflow-runs?limit=50");
  return (
    <>
      <PageTitle
        eyebrow="PROJECT EXECUTION"
        title="Project workflow runs"
        description="Follow each execution from acceptance through its stages. Run completion does not imply that a pull request was created or approved."
      />
      <Failure error={runs.error} />
      <Section title="Latest 50 runs">
        {runs.isPending ? (
          <p className="editor-body">Loading runs…</p>
        ) : runs.data ? (
          <ProjectRunTable runs={runs.data} />
        ) : null}
      </Section>
    </>
  );
}
export function ProjectRunPage() {
  const { runId } = useParams();
  return <RunDetail key={runId} id={runId ?? ""} />;
}
function RunDetail({ id }: { id: string }) {
  const project = useProjectId(),
    run = useProjectData<ProjectRun>("/workflow-runs/" + id);
  const [params, setParams] = useSearchParams();
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [pending, setPending] = useState(false);
  const { write, alive } = useWorkflowWrite();
  const value = run.data;
  const stage = params.get("stage") ?? "",
    execution = params.get("execution") ?? "";
  const filters = new URLSearchParams({ run_id: id });
  for (const [key, value] of [
    ["stage", stage],
    ["execution_id", execution],
    ["worker_id", params.get("worker") ?? ""],
    ["provider", params.get("provider") ?? ""],
    ["model", params.get("model") ?? ""],
  ] as const)
    if (value) filters.set(key, value);
  const query = filters.toString();
  return (
    <>
      <PageTitle
        eyebrow="PINNED PROJECT RUN"
        title={value ? humanWorkflowName(value.name) : "Workflow run"}
        description={
          value
            ? "Inspect stage execution, model exchanges and captured output for this run."
            : "Loading canonical execution…"
        }
      />
      <Failure error={run.error || error} />
      {value && (
        <>
          <div className="project-run-summary">
            <div className="project-summary-tile">
              <span>Run status</span>
              <Status value={value.state} />
              <small>
                Controller state; inspect stage evidence for the outcome.
              </small>
            </div>
            <div className="project-summary-tile">
              <span>Created</span>
              <strong>{localTimestamp(value.created_at)}</strong>
              <small>Time the run was accepted</small>
            </div>
            <div className="project-summary-tile">
              <span>Dispatch</span>
              <Status value={value.dispatch.state} />
              <small>{value.stages?.length ?? 0} declared stages</small>
            </div>
            <div className="project-summary-tile">
              <span>Repository</span>
              <strong>{value.default_repository ?? "Not reported"}</strong>
              <small>Pinned for this execution</small>
            </div>
          </div>
          <details className="setup-advanced">
            <summary>Advanced run identifiers</summary>
            <dl className="project-facts">
              <dt>Run</dt>
              <dd>{id}</dd>
              <dt>Snapshot</dt>
              <dd>{value.snapshot_id}</dd>
              <dt>Factory revision</dt>
              <dd>{value.factory_revision_id ?? "Not reported"}</dd>
              <dt>Engine run</dt>
              <dd>{value.dispatch.engine_id ?? "Not submitted"}</dd>
            </dl>
          </details>
          {["queued", "running", "cancelling"].includes(value.state) && (
            <p>Request accepted; execution is not complete.</p>
          )}
          <Failure error={value.dispatch.error} />
          {message && <p role="status">{message}</p>}
          <Link
            to={projectPath(
              project,
              "/workflow-revisions/" + value.revision_id,
            )}
          >
            Inspect pinned workflow revision
          </Link>
          <button
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
                    ["cancelled", "completed", "failed"].includes(receipt.state)
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
          {value.state === "cancelling" && (
            <p>Cancellation requested; execution may still be running.</p>
          )}
          <nav className="project-run-jumps" aria-label="Run sections">
            <a href="#run-stages">Workflow stages</a>
            <a href="#run-model-calls">Model exchanges</a>
            <a href="#run-activity">Execution activity</a>
          </nav>
          <div id="run-stages">
            <Section title="Workflow stages">
              <nav
                className="project-stage-track"
                aria-label="Select workflow stage"
              >
                <button
                  type="button"
                  aria-pressed={!stage}
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.delete("stage");
                    next.delete("execution");
                    setParams(next);
                  }}
                >
                  All stages
                </button>
                {value.stages?.map((s, i) => {
                  const attempt = value.attempts
                    ?.filter((a) => a.stage_id === s.id)
                    .sort((a, b) => b.generation - a.generation)[0];
                  return (
                    <button
                      key={s.id}
                      type="button"
                      aria-pressed={stage === s.id}
                      onClick={() => {
                        const next = new URLSearchParams(params);
                        next.set("stage", s.id);
                        next.delete("execution");
                        setParams(next);
                      }}
                    >
                      <span className="project-stage-number">{i + 1}</span>
                      <span>
                        {s.id.replaceAll("_", " ")}
                        <small>
                          {attempt
                            ? "Latest attempt: " + attempt.state
                            : "No attempt yet"}
                        </small>
                      </span>
                    </button>
                  );
                })}
              </nav>
              <div className="editor-body">
                <p>
                  Edges follow declared direct dependencies; input references
                  are shown in each stage's context. No execution state is
                  inferred from dispatch acceptance.
                </p>
                {value.stages
                  ?.filter((s) => !stage || s.id === stage)
                  .map((s) => (
                    <article className="workflow-stage" key={s.id}>
                      <h3>
                        {s.id.replaceAll("_", " ")}
                        <span className="project-stage-kind">{s.executor}</span>
                      </h3>
                      <p className="project-stage-action">
                        {s.action ?? s.image ?? "Action not reported"}
                      </p>
                      <p>Depends on: {s.after?.join(", ") || "start"}</p>
                      <details className="setup-advanced">
                        <summary>Advanced stage configuration</summary>
                        {s.when != null && (
                          <p>Condition: {JSON.stringify(s.when)}</p>
                        )}
                        <p>
                          Repository {s.repository_alias} · {s.repository_id}
                          <br />
                          Repository revision {s.repository_revision_id} ·
                          configuration {s.config_revision_id}
                          <br />
                          Pool {s.pool_id} · timeout{" "}
                          {s.timeout_seconds ?? "unknown"} seconds · configured
                          retries {s.retries ?? "unknown"}
                        </p>
                        <details>
                          <summary>Pinned context for {s.id}</summary>
                          <pre>
                            {JSON.stringify(
                              {
                                settings: s.settings,
                                inputs: s.inputs,
                                image: s.image,
                                command: s.command,
                              },
                              null,
                              2,
                            )}
                          </pre>
                        </details>
                      </details>
                      {value.attempts
                        ?.filter((a) => a.stage_id === s.id)
                        .map((a) => (
                          <div className="workflow-attempt" key={a.id}>
                            <strong>Attempt {a.generation}</strong> ·{" "}
                            <Status value={a.state} />
                            <details className="setup-advanced">
                              <summary>Execution identifier</summary>
                              <code>{a.id}</code>
                            </details>
                            <br />
                            <Link
                              to={
                                projectPath(project, "/runs/" + id) +
                                "?" +
                                new URLSearchParams({
                                  stage: s.id,
                                  execution: a.id,
                                })
                              }
                            >
                              Inspect attempt {a.generation} of {s.id}
                            </Link>
                            {a.worker_id && (
                              <>
                                {" "}
                                ·{" "}
                                <Link
                                  to={projectPath(
                                    project,
                                    "/workers/" + a.worker_id,
                                  )}
                                >
                                  Worker {a.worker_id}
                                </Link>
                              </>
                            )}
                            {a.job_id && (
                              <>
                                {" "}
                                ·{" "}
                                <Link
                                  to={
                                    projectPath(project, "/jobs/" + a.job_id) +
                                    "?" +
                                    new URLSearchParams({ run: id })
                                  }
                                >
                                  Job {a.job_id}
                                </Link>
                              </>
                            )}
                          </div>
                        ))}
                      {!value.attempts?.some((a) => a.stage_id === s.id) && (
                        <p>No attempt record yet.</p>
                      )}
                    </article>
                  ))}
              </div>
            </Section>
          </div>
          <Section title="Follow-up workflows">
            <div className="editor-body">
              {value.followups?.length
                ? value.followups.map((f) => (
                    <article className="project-followup-card" key={f.id}>
                      <div>
                        <strong>{f.kind.replaceAll("_", " ")}</strong>
                        <Status value={f.state} />
                      </div>
                      {f.result_run_id && (
                        <Link
                          to={projectPath(project, "/runs/" + f.result_run_id)}
                        >
                          Inspect follow-up run
                        </Link>
                      )}
                      <details>
                        <summary>Dispatch details</summary>
                        <dl className="project-facts">
                          <dt>Receipt</dt>
                          <dd>{f.id}</dd>
                          <dt>Dispatch</dt>
                          <dd>{f.dispatch?.state ?? "Not reported"}</dd>
                          <dt>Engine</dt>
                          <dd>{f.dispatch?.engine_id ?? "Not submitted"}</dd>
                        </dl>
                      </details>
                      <Failure error={f.error} />
                    </article>
                  ))
                : "No follow-up receipts."}
            </div>
          </Section>
          {((stage && !value.stages?.some((s) => s.id === stage)) ||
            (execution &&
              !value.attempts?.some((a) => a.id === execution))) && (
            <Failure error="Unknown stage or execution for this run." />
          )}
          <Section title="History filters">
            <div className="editor-body">
              <p>
                Stage {stage || "all"} · execution {execution || "all"}{" "}
                <Link to={projectPath(project, "/runs/" + id)}>
                  Clear filters
                </Link>
              </p>
              {[
                ["worker", "Worker ID"],
                ["provider", "Provider"],
                ["model", "Model"],
              ].map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    value={params.get(key) ?? ""}
                    onChange={(e) => {
                      const next = new URLSearchParams(params);
                      if (e.target.value) next.set(key, e.target.value);
                      else next.delete(key);
                      setParams(next, { replace: true });
                    }}
                  />
                </label>
              ))}
            </div>
          </Section>
          <ProjectHistory key={query} query={query} />
        </>
      )}
    </>
  );
}
export function ProjectJobPage() {
  const { jobId } = useParams();
  const project = useProjectId(),
    job = useProjectData<ProjectJob>("/jobs/" + jobId);
  const [params] = useSearchParams();
  const result = job.data?.result;
  return (
    <>
      <PageTitle
        eyebrow="ACTION EXECUTION"
        title={"Job " + jobId}
        description="Durable broker receipt, deadline and bounded capture. CPU and memory are not AI usage."
      />
      <Failure error={job.error} />
      {job.data && (
        <>
          <p>
            <Status value={job.data.state} /> · execution{" "}
            {job.data.execution_id}
            <br />
            Deadline {date(job.data.deadline)}
            <br />
            Exit code: {result?.exit_code ?? "Unknown"} · OOM:{" "}
            {result?.oom === undefined ? "Unknown" : String(result.oom)}
          </p>
          <p>
            Target: {job.data.target_id ?? "local-docker"} ·{" "}
            {job.data.runtime?.kind ?? "docker"}
          </p>
          {job.data.runtime?.namespace && (
            <p>
              Namespace: <span>{job.data.runtime.namespace}</span>
            </p>
          )}
          {job.data.runtime?.job_uid && (
            <p>
              Job UID: <span>{job.data.runtime.job_uid}</span>
            </p>
          )}
          {job.data.runtime?.pod_uids?.map((uid) => (
            <p key={uid}>
              Pod UID: <span>{uid}</span>
            </p>
          ))}
          <p>
            Runtime phase: {job.data.runtime?.phase ?? "unknown"} ·{" "}
            {job.data.runtime?.cleanup_pending
              ? "Cleanup pending"
              : "No cleanup pending"}
          </p>
          {job.data.cancel_requested && (
            <p>
              Cancellation requested; terminal cleanup must still be confirmed
              by job state.
            </p>
          )}
          <Failure error={job.data.error || result?.error} />
          {params.get("run") && (
            <Link to={projectPath(project, "/runs/" + params.get("run"))}>
              Return to project run
            </Link>
          )}
          {!result && <p>No captured result yet.</p>}
          {(job.data.runtime?.log_stream === "combined"
            ? (["stdout"] as const)
            : (["stdout", "stderr"] as const)
          ).map((stream) => (
            <Section
              key={stream}
              title={
                job.data.runtime?.log_stream === "combined"
                  ? "Combined action logs"
                  : stream
              }
            >
              <div className="editor-body">
                <pre>
                  {result?.[stream]?.slice(0, 65536) || "No captured output."}
                </pre>
                {(result?.[(stream + "_truncated") as "stdout_truncated"] ||
                  (result?.[stream]?.length ?? 0) > 65536) && (
                  <p>
                    Output truncated. This view shows at most 65,536 characters
                    per stream.
                  </p>
                )}
              </div>
            </Section>
          ))}
          <Section title="Declared job result and AI usage">
            <pre>
              {JSON.stringify(
                {
                  output: result?.output ?? null,
                  usage: result?.usage ?? null,
                },
                null,
                2,
              ).slice(0, 65536)}
            </pre>
            <p>
              Only explicitly reported model usage contributes to AI accounting.
              Unknown is not zero. Result preview is limited to 65,536
              characters.
            </p>
          </Section>
          <ProjectHistory
            query={new URLSearchParams({
              execution_id: job.data.execution_id,
              job_id: job.data.job_id,
            }).toString()}
          />
        </>
      )}
    </>
  );
}
export function ProjectWorkerPage() {
  const { workerId } = useParams();
  const workers = useData<Workers>("workers", "/workers");
  const worker = workers.data?.managed.find((w) => w.id === workerId);
  return (
    <>
      <PageTitle
        eyebrow="GLOBAL WORKER / PROJECT HISTORY"
        title={"Worker " + (worker?.name ?? workerId)}
        description="Infrastructure is shared. The history and accounting below remain scoped to the project selected in the sidebar."
      />
      <Failure error={workers.error} />
      <div className="project-run-summary">
        <div className="project-summary-tile">
          <span>Desired state</span>
          <Status value={worker?.desired_state ?? "unknown"} />
        </div>
        <div className="project-summary-tile">
          <span>Runtime</span>
          <Status value={worker?.runtime?.status ?? "unknown"} />
        </div>
        <div className="project-summary-tile">
          <span>Last heartbeat</span>
          <strong>{localTimestamp(worker?.engine?.last_heartbeat)}</strong>
          <small>
            Engine observation; this is not the worker's start time.
          </small>
        </div>
      </div>
      <ProjectHistory
        query={new URLSearchParams({ worker_id: workerId ?? "" }).toString()}
      />
    </>
  );
}
function ProjectHistory({ query }: { query: string }) {
  const events = useProjectData<FactoryEvent[]>(
    "/events?" + query + "&limit=200",
  );
  return (
    <>
      <ProjectAccounting query={query} />
      <Failure error={events.error} />
      <div id="run-model-calls">
        {" "}
        <ProjectCalls
          key={query}
          events={
            events.data?.filter(
              (e): e is ModelAccountingEvent => e.kind === "model.completed",
            ) ?? []
          }
        />
      </div>
      <details id="run-activity" className="project-activity-group">
        <summary>
          Execution activity ·{" "}
          {events.data?.filter((event) => event.kind === "log").length ?? 0}{" "}
          retained logs
        </summary>
        <Section title="Bounded execution logs">
          <div className="editor-body">
            <p>
              Latest 200 events; capture and retention policies can leave gaps.
              Logs are not used to infer model spend.
            </p>
            <ProjectActivity events={events.data ?? []} />
          </div>
        </Section>
      </details>
    </>
  );
}
export function ProjectCalls({ events }: { events: ModelAccountingEvent[] }) {
  return <ProjectModelCalls events={events} />;
}
