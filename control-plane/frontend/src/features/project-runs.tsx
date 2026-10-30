import { Link, useParams, useSearchParams } from "react-router-dom";
import type { FactoryEvent, Workers } from "../api";
import { date, Failure, PageTitle, Section, Status } from "../components/ui";
import { useData } from "../hooks";
import { projectPath, useProjectData, useProjectId } from "../project-context";
import type {
  ProjectRun,
  ProjectJob,
  ModelAccountingEvent,
} from "../workflow-api";
import { ProjectAccounting } from "./usage";
import { ProjectActivity } from "../components/project-activity";
import { ProjectRunTable } from "../components/project-run-table";
import { localTimestamp } from "../components/project-format";
import { ProjectModelCalls } from "./project-model-calls";
import { ProjectRunInspector } from "./project-run-inspector";
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
  return <ProjectRunInspector key={runId} id={runId ?? ""} />;
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
