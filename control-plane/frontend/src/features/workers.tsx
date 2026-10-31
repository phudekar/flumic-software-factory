import { useQueryClient } from "@tanstack/react-query";
import {
  Box,
  RefreshCw,
  Play,
  Search,
  ShieldCheck,
  Square,
  Terminal,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import type { FactoryEvent, WorkerBuild } from "../api";
import { ScopedUsage } from "./runs";
import { RuntimeLogView } from "../components/logs";
import { date } from "../components/ui";
import type { Workers } from "../api";
import { api } from "../api";
import { Empty, Failure, PageTitle, Section, Status } from "../components/ui";
import { useData } from "../hooks";

import { SharedWorkerForm } from "./runtime-targets";

export function WorkersPage() {
  const data = useData<Workers>("workers", "/workers");
  const [sharedCreating, setSharedCreating] = useState(false);
  const [showRemoved, setShowRemoved] = useState(false);
  const [showExited, setShowExited] = useState(false);
  const visibleDiscovered =
    data.data?.discovered.filter(
      (worker) => showExited || worker.status !== "exited",
    ) ?? [];
  const visibleWorkers =
    data.data?.managed.filter(
      (worker) => showRemoved || worker.desired_state !== "removed",
    ) ?? [];
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [logs, setLogs] = useState<string | null>(null);
  const client = useQueryClient();
  async function action(id: string, verb: string) {
    if (
      (verb === "stop" || verb === "remove" || verb === "restart") &&
      !window.confirm(
        verb === "stop" || verb === "restart"
          ? "This can interrupt active tasks, which Hatchet may retry. Continue?"
          : "Remove the stopped worker runtime? Its history will remain.",
      )
    )
      return;
    setPending(true);
    setError("");
    try {
      await api("/workers/" + id + "/actions", { action: verb });
      await client.invalidateQueries({ queryKey: ["workers"] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      {sharedCreating && (
        <SharedWorkerForm close={() => setSharedCreating(false)} />
      )}
      <PageTitle
        eyebrow="EXECUTION INFRASTRUCTURE"
        title="Workers"
        description="Manage worker instances without coupling your factory to a runtime."
        action={
          <div>
            <button onClick={() => setSharedCreating(true)}>
              Add shared worker
            </button>
          </div>
        }
      />
      <p>
        Worker instances are deployment infrastructure.{" "}
        <Link to="/projects">Choose a project</Link> to inspect shared pool
        membership and loaded action versions.
      </p>
      <Failure error={error || data.error} />
      {data.data?.warnings.map((x) => (
        <div key={x} className="notice">
          {x}
        </div>
      ))}
      {data.data?.managed.some(
        (w) => w.runtime?.protocol_current === false,
      ) && (
        <div className="notice">
          A worker runtime update is available. Restart through the control
          plane to use configurable file limits and complete error capture.
          Running workers have been left unchanged.
        </div>
      )}
      <div className="toolbar">
        <label className="search">
          <Search size={15} />
          <input
            placeholder="Filter by name or role…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <label className="inline-label">
          <input
            type="checkbox"
            checked={showRemoved}
            onChange={(event) => setShowRemoved(event.target.checked)}
          />
          Show removed workers
        </label>
        <label className="inline-label">
          <input
            type="checkbox"
            checked={showExited}
            onChange={(event) => setShowExited(event.target.checked)}
          />
          Show exited discovered workers
        </label>
        <span className="tag">
          <Box size={13} />
          Runtime infrastructure
        </span>
        <span className="muted">Refreshes every 10 seconds</span>
      </div>
      <Section
        title="Managed instances"
        aside={<span className="tag">{visibleWorkers.length} workers</span>}
      >
        {!visibleWorkers.length ? (
          <Empty
            title="Your fleet starts here"
            detail="Add a shared worker to a pool, then start it explicitly when ready."
          />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Worker / role</th>
                  <th>Runtime</th>
                  <th>Hatchet</th>
                  <th>Config revision</th>
                  <th>Last operation</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {visibleWorkers
                  .filter((w) => (w.name + " " + w.profile).includes(search))
                  .map((w) => (
                    <tr key={w.id}>
                      <td>
                        <Link className="text-link" to={"/workers/" + w.id}>
                          {w.name}
                        </Link>
                        <small>{w.profile}</small>
                      </td>
                      <td>
                        <Status
                          value={
                            w.desired_state === "removed"
                              ? "removed"
                              : (w.runtime?.status ??
                                (w.desired_state === "created"
                                  ? "created"
                                  : "unknown"))
                          }
                        />
                      </td>
                      <td>
                        {w.engine ? (
                          <Status
                            value={w.engine.paused ? "paused" : "registered"}
                          />
                        ) : (
                          <span className="muted">Not observed</span>
                        )}
                      </td>
                      <td className="mono">{w.revision_id.slice(0, 8)}</td>
                      <td>
                        {w.operations[0] ? (
                          <>
                            <Status value={w.operations[0].status} />
                            {w.build && w.build.status !== "not_requested" && (
                              <small>Image: {w.build.status}</small>
                            )}
                            {w.operations[0].error && (
                              <small className="error-text">
                                {w.operations[0].error}
                              </small>
                            )}
                          </>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>
                        <div className="actions">
                          <button
                            disabled={
                              pending ||
                              w.desired_state === "removed" ||
                              w.runtime?.status === "running" ||
                              w.operations.some((o) =>
                                ["pending", "running"].includes(o.status),
                              )
                            }
                            onClick={() => action(w.id, "start")}
                            title="Start worker"
                          >
                            <Play size={13} />
                          </button>
                          <button
                            disabled={
                              pending || w.runtime?.status !== "running"
                            }
                            onClick={() => action(w.id, "stop")}
                            title="Stop worker"
                          >
                            <Square size={13} />
                          </button>
                          <button
                            disabled={
                              pending ||
                              !w.runtime ||
                              w.operations.some((o) =>
                                ["pending", "running"].includes(o.status),
                              )
                            }
                            title="Restart shared worker"
                            onClick={() => action(w.id, "restart")}
                          >
                            <RefreshCw size={13} />
                          </button>
                          <Link
                            className="text-link"
                            to={"/workers/" + w.id + "#image-build"}
                          >
                            Build logs
                          </Link>
                          <button
                            disabled={!w.runtime}
                            onClick={async () => {
                              try {
                                const result = await api<{ content: string }>(
                                  "/runtime/" + w.runtime!.id + "/logs",
                                );
                                setLogs(result.content);
                              } catch (e) {
                                setError((e as Error).message);
                              }
                            }}
                            title="Startup logs"
                          >
                            <Terminal size={13} />
                          </button>
                          <button
                            disabled={
                              pending ||
                              w.desired_state === "removed" ||
                              w.runtime?.status === "running" ||
                              w.operations.some((o) =>
                                ["pending", "running"].includes(o.status),
                              )
                            }
                            onClick={() => action(w.id, "remove")}
                            title="Remove stopped runtime"
                          >
                            <X size={13} />
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
      <Section
        title="Discovered Compose workers"
        aside={<span className="tag">Read only</span>}
      >
        {!visibleDiscovered.length ? (
          <Empty
            title="No visible Compose workers"
            detail="Only this factory's configured Compose services are included. Enable Show exited discovered workers to include stopped containers."
          />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Container</th>
                  <th>Status</th>
                  <th>Image</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {visibleDiscovered.map((w) => (
                  <tr key={w.id}>
                    <td>
                      <strong>{w.name}</strong>
                      <small className="mono">{w.id.slice(0, 12)}</small>
                    </td>
                    <td>
                      <Status value={w.status} />
                    </td>
                    <td className="muted mono">{w.image}</td>
                    <td>
                      <button
                        className="secondary"
                        onClick={async () => {
                          try {
                            const r = await api<{ content: string }>(
                              "/runtime/" + w.id + "/logs",
                            );
                            setLogs(r.content);
                          } catch (e) {
                            setError((e as Error).message);
                          }
                        }}
                      >
                        <Terminal size={13} />
                        Logs
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <div className="notice">
        <ShieldCheck size={16} />
        Graceful drain is unavailable until Hatchet pause and assignment-race
        behavior is verified. Stop can interrupt active work.
      </div>
      {logs !== null && (
        <div className="modal-backdrop">
          <div className="modal wide">
            <div className="panel-heading">
              <h2>Runtime logs</h2>
              <button
                className="icon-button"
                onClick={() => setLogs(null)}
                aria-label="Close"
              >
                <X />
              </button>
            </div>
            <RuntimeLogView content={logs} />
          </div>
        </div>
      )}
    </>
  );
}

export function WorkerPage() {
  const { id } = useParams();
  const workers = useData<Workers>("workers", "/workers");
  const query = "worker_id=" + encodeURIComponent(id ?? "");
  const events = useData<FactoryEvent[]>(
    "worker-events",
    "/events?" + query + "&kind=attempt&limit=500",
  );
  const worker = workers.data?.managed.find((w) => w.id === id);
  const attempts = new Map<string, FactoryEvent>();
  for (const event of events.data ?? []) {
    if (
      (event.kind === "attempt.started" ||
        event.kind === "attempt.completed") &&
      event.attempt_id &&
      !attempts.has(event.attempt_id)
    )
      attempts.set(event.attempt_id, event);
  }
  return (
    <>
      <Link className="text-link" to="/workers">
        ← Workers
      </Link>
      <PageTitle
        eyebrow="WORKER EXECUTION"
        title={worker?.name ?? "Worker"}
        description="Navigate from this worker to its workflow stages, retries and model usage."
      />
      <Failure error={events.error || workers.error} />
      {worker && (
        <Section title="Runtime observation">
          <div className="editor-body">
            <p>
              Target: {worker.target_id} · Desired state: {worker.desired_state}
            </p>
            <p>Runtime observation: {worker.runtime?.status ?? "unknown"}</p>
            <p>
              Hatchet registration:{" "}
              {worker.engine
                ? worker.engine.paused
                  ? "Paused"
                  : "Registered"
                : "Not observed"}
            </p>
            {worker.runtime?.namespace && (
              <p>
                Namespace: <span>{worker.runtime.namespace}</span>
              </p>
            )}
            {worker.runtime?.deployment_uid && (
              <p>
                Deployment UID: <span>{worker.runtime.deployment_uid}</span>
              </p>
            )}
            {worker.runtime?.pod_uids?.map((uid) => (
              <p key={uid}>
                Pod UID: <span>{uid}</span>
              </p>
            ))}
            <p>Actual image: {worker.runtime?.image ?? "Not observed"}</p>
            <p>
              A ready process and Hatchet registration are separate
              observations.
            </p>
          </div>
        </Section>
      )}
      <WorkerBuildView id={id ?? ""} />
      <ScopedUsage query={query} />
      <Section
        title="Executed stage attempts"
        aside={<span className="tag">Latest 500 worker events</span>}
      >
        {!attempts.size ? (
          <Empty
            title="No captured stage attempts"
            detail="Execution history appears when an instrumented worker runs a stage. Existing containers do not gain past telemetry retroactively."
          />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Stage</th>
                  <th>Attempt</th>
                  <th>Result</th>
                  <th>Last event</th>
                  <th>Workflow</th>
                </tr>
              </thead>
              <tbody>
                {[...attempts.values()].map((e) => {
                  const run = String(e.payload.engine_workflow_run_id ?? "");
                  const task = String(e.payload.engine_task_run_id ?? "");
                  const params = new URLSearchParams({
                    task,
                    attempt: e.attempt_id ?? "",
                    worker: id ?? "",
                  });
                  if (!task && e.stage) params.set("stage", e.stage);
                  return (
                    <tr key={e.attempt_id}>
                      <td>{e.stage}</td>
                      <td className="mono">{e.attempt_id?.slice(0, 8)}</td>
                      <td>
                        <Status
                          value={String(e.payload.outcome ?? "started")}
                        />
                      </td>
                      <td>{date(e.occurred_at)}</td>
                      <td>
                        {run ? (
                          <div className="actions">
                            <Link
                              className="text-link"
                              to={"/runs/" + run + "?" + params}
                            >
                              Inspect stage →
                            </Link>
                            <Link
                              className="text-link"
                              to={"/runs/" + run + "?" + params + "&tab=logs"}
                            >
                              View logs →
                            </Link>
                          </div>
                        ) : (
                          <span className="muted">
                            Engine correlation unavailable
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {(events.data?.length ?? 0) === 500 && (
          <div className="notice">
            History is limited to the latest 500 events; older attempts may be
            omitted.
          </div>
        )}
      </Section>
    </>
  );
}

function WorkerBuildView({ id }: { id: string }) {
  const build = useData<WorkerBuild>(
    "worker-build",
    "/workers/" + encodeURIComponent(id) + "/build",
  );
  return (
    <div id="image-build">
      <Section title="Worker image build">
        <Failure error={build.error} />
        {build.data && build.data.status !== "not_requested" ? (
          <>
            <Status value={build.data.status} />
            {build.data.image && <p className="mono">{build.data.image}</p>}
            <p className="muted">
              Build logs refresh every 10 seconds. Image setup runs during
              build; startup scripts run when the worker starts.
            </p>
            <RuntimeLogView content={build.data.content ?? ""} />
          </>
        ) : (
          <p className="muted">
            No image build recorded. Profiles without a base image use a
            prebuilt worker image.
          </p>
        )}
      </Section>
    </div>
  );
}
