import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { FactoryEvent, Workers } from "../api";
import {
  date,
  Empty,
  Failure,
  PageTitle,
  Section,
  Status,
  count,
  money,
} from "../components/ui";
import { useData } from "../hooks";
import { projectPath, useProjectData, useProjectId } from "../project-context";
import { diagnostic, useWorkflowWrite } from "../workflow-api";
import type { ProjectRun, ProjectJob } from "../workflow-api";
import { ProjectAccounting } from "./usage";
export function ProjectRunsPage() {
  const project = useProjectId(),
    runs = useProjectData<ProjectRun[]>("/workflow-runs?limit=50");
  return (
    <>
      <PageTitle
        eyebrow="PROJECT EXECUTION"
        title="Project workflow runs"
        description="Canonical run state and durable dispatch receipts; acceptance is not completion."
      />
      <Failure error={runs.error} />
      <Section title="Latest 50 runs">
        <div className="editor-body">
          {runs.isPending
            ? "Loading runs…"
            : runs.data?.length
              ? runs.data.map((run) => (
                  <p key={run.id}>
                    <Link to={projectPath(project, "/runs/" + run.id)}>
                      {run.name} · {run.id}
                    </Link>{" "}
                    · <Status value={run.state} /> · dispatch{" "}
                    {run.dispatch.state} · revision {run.revision_id}
                  </p>
                ))
              : "No project runs yet."}
        </div>
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
        title={"Run " + id}
        description={value ? value.name : "Loading canonical execution…"}
      />
      <Failure error={run.error || error} />
      {value && (
        <>
          <p>
            <Status value={value.state} /> · Dispatch {value.dispatch.state}
            <br />
            Snapshot {value.snapshot_id} · factory revision{" "}
            {value.factory_revision_id}
            <br />
            Engine run: {value.dispatch.engine_id ?? "not submitted"}
          </p>
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
            Pinned workflow revision {value.revision_id}
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
                      : "Cancellation requested; execution may still be running.",
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
          {value.state === "cancelling" &&
            message !==
              "Cancellation requested; execution may still be running." && (
              <p>Cancellation requested; execution may still be running.</p>
            )}
          <Section title="Pinned dependency graph">
            <div className="editor-body">
              <p>
                Edges follow declared direct dependencies; input references are
                shown in each stage's context. No execution state is inferred
                from dispatch acceptance.
              </p>
              {value.stages?.map((s) => (
                <article className="workflow-stage" key={s.id}>
                  <h3>
                    {s.id} ·{" "}
                    {s.executor.startsWith("docker") ? "Docker" : "Builtin"} ·{" "}
                    {s.action ?? s.image}
                  </h3>
                  <p>Depends on: {s.after?.join(", ") || "start"}</p>
                  {s.when != null && <p>Condition: {JSON.stringify(s.when)}</p>}
                  <p>
                    Repository {s.repository_alias} · {s.repository_id}
                    <br />
                    Repository revision {s.repository_revision_id} ·
                    configuration {s.config_revision_id}
                    <br />
                    Pool {s.pool_id} · timeout {s.timeout_seconds ?? "unknown"}{" "}
                    seconds · configured retries {s.retries ?? "unknown"}
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
                  {value.attempts
                    ?.filter((a) => a.stage_id === s.id)
                    .map((a) => (
                      <div className="workflow-attempt" key={a.id}>
                        <strong>Attempt {a.generation}</strong> ·{" "}
                        <Status value={a.state} /> · execution {a.id}
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
                              Docker job {a.job_id}
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
          <Section title="Follow-up workflows">
            <div className="editor-body">
              {value.followups?.length
                ? value.followups.map((f) => (
                    <p key={f.id}>
                      {f.kind} · {f.state} · engine{" "}
                      {f.dispatch?.engine_id ?? "not submitted"}{" "}
                      {f.result_run_id && (
                        <Link
                          to={projectPath(project, "/runs/" + f.result_run_id)}
                        >
                          Child run {f.result_run_id}
                        </Link>
                      )}
                      <Failure error={f.error} />
                    </p>
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
        eyebrow="DOCKER EXECUTION"
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
          {(["stdout", "stderr"] as const).map((stream) => (
            <Section key={stream} title={stream}>
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
      <p>
        Desired state {worker?.desired_state ?? "unknown"} · runtime{" "}
        {worker?.runtime?.status ?? "unknown"}
      </p>
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
      <Section title="Bounded execution logs">
        <div className="editor-body">
          <p>
            Latest 200 events; capture and retention policies can leave gaps.
            Logs are not used to infer model spend.
          </p>
          <pre>
            {events.data
              ?.filter((e) => e.kind === "log")
              .map((e) => date(e.occurred_at) + " " + JSON.stringify(e.payload))
              .join("\n")
              .slice(0, 65536) || "No captured logs."}
          </pre>
        </div>
      </Section>
      <ProjectCalls
        events={events.data?.filter((e) => e.kind === "model.completed") ?? []}
      />
    </>
  );
}
function ProjectCalls({ events }: { events: FactoryEvent[] }) {
  const project = useProjectId(),
    [selected, setSelected] = useState<string | null>(null);
  const transcript = useQuery<Record<string, unknown>>({
    queryKey: ["project", project, "conversation", selected],
    queryFn: ({ signal }) =>
      api(
        projectPath(project, "/model-calls/" + selected + "/conversation"),
        undefined,
        { signal },
      ),
    enabled: !!selected,
    refetchInterval: false,
  });
  return (
    <Section title="Project model calls">
      <div className="editor-body">
        <p>
          Captured content expires after seven days. Metadata-only or disabled
          capture may omit exchanges. Parent summaries are excluded from
          aggregate totals.
        </p>
        {events.length ? (
          events.map((e) => (
            <article key={e.id}>
              <strong>
                {String(e.payload.provider ?? "Unknown provider")} /{" "}
                {String(e.payload.model ?? "Unknown model")}
              </strong>
              <p>
                Stage {e.stage ?? "unknown"} · tokens{" "}
                {count(e.payload.input_tokens as number | null)} /{" "}
                {count(e.payload.output_tokens as number | null)} · estimated{" "}
                {money(e.payload.estimated_usd as number | null)} · reported{" "}
                {money(e.payload.reported_usd as number | null)}
              </p>
              {e.payload.accounting_leaf === false && (
                <p>Parent summary — excluded from totals</p>
              )}
              <button
                disabled={!e.payload.conversation_available}
                onClick={() => setSelected(e.id)}
              >
                View exchange {e.id}
              </button>
            </article>
          ))
        ) : (
          <Empty
            title="No captured model calls"
            detail="No retained model records match these filters."
          />
        )}
        {selected && (
          <>
            <button onClick={() => setSelected(null)}>Close exchange</button>
            <Failure error={transcript.error} />
            {transcript.isPending && <p>Loading exchange…</p>}
            {transcript.data && (
              <pre>{JSON.stringify(transcript.data, null, 2)}</pre>
            )}
          </>
        )}
      </div>
    </Section>
  );
}
