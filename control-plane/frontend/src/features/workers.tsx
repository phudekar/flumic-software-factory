import { useQueryClient } from "@tanstack/react-query";
import {
  Box,
  FileSliders,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Square,
  Terminal,
  X,
} from "lucide-react";
import { useState } from "react";
import type { Operation, Revision, Worker, Workers } from "../api";
import { api } from "../api";
import { Empty, Failure, PageTitle, Section, Status } from "../components/ui";
import { useData } from "../hooks";

export function WorkersPage() {
  const data = useData<Workers>("workers", "/workers");
  const revisions = useData<Revision[]>("revisions", "/config/revisions");
  const latest = revisions.data?.[0];
  const config = useData<Revision>(
    "revision",
    "/config/revisions/" + (latest?.id ?? "unavailable"),
  );
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [profile, setProfile] = useState("simple");
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [logs, setLogs] = useState<string | null>(null);
  const [configWorker, setConfigWorker] = useState<Worker | null>(null);
  const [applyRevision, setApplyRevision] = useState("");
  const client = useQueryClient();
  async function action(id: string, verb: string) {
    if (
      (verb === "stop" || verb === "remove") &&
      !window.confirm(
        verb === "stop"
          ? "Stop this worker? Active tasks may be interrupted and retried by Hatchet."
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
      <PageTitle
        eyebrow="EXECUTION INFRASTRUCTURE"
        title="Workers"
        description="Manage worker instances without coupling your factory to a runtime."
        action={
          <button className="primary" onClick={() => setCreating(true)}>
            <Plus size={15} />
            Create worker
          </button>
        }
      />
      <Failure error={error || data.error} />
      {data.data?.warnings.map((x) => (
        <div key={x} className="notice">
          {x}
        </div>
      ))}
      <div className="toolbar">
        <label className="search">
          <Search size={15} />
          <input
            placeholder="Filter by name or profile…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <span className="tag">
          <Box size={13} />
          Local Docker
        </span>
        <span className="muted">Refreshes every 10 seconds</span>
      </div>
      <Section
        title="Managed instances"
        aside={
          <span className="tag">{data.data?.managed.length ?? 0} workers</span>
        }
      >
        {!data.data?.managed.length ? (
          <Empty
            title="Your fleet starts here"
            detail="Create a worker from a configuration revision. Workers are created stopped and join Hatchet only when you start them."
          />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Worker / profile</th>
                  <th>Runtime</th>
                  <th>Hatchet</th>
                  <th>Config revision</th>
                  <th>Last operation</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.data.managed
                  .filter((w) => (w.name + " " + w.profile).includes(search))
                  .map((w) => (
                    <tr key={w.id}>
                      <td>
                        <strong>{w.name}</strong>
                        <small>{w.profile}</small>
                      </td>
                      <td>
                        <Status
                          value={
                            w.runtime?.status ??
                            (w.desired_state === "created"
                              ? "created"
                              : "unknown")
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
                              !w.runtime ||
                              w.runtime.status === "running" ||
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
                              pending || w.runtime?.status === "running"
                            }
                            title="Apply configuration to stopped worker"
                            onClick={() => {
                              setConfigWorker(w);
                              setApplyRevision(latest?.id ?? w.revision_id);
                            }}
                          >
                            <FileSliders size={13} />
                          </button>
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
                              !w.runtime ||
                              w.runtime.status === "running"
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
        {!data.data?.discovered.length ? (
          <Empty
            title="No Compose workers discovered"
            detail="Only this factory's configured Compose services are included."
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
                {data.data.discovered.map((w) => (
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
      {creating && (
        <div className="modal-backdrop">
          <form
            className="modal"
            onSubmit={async (e) => {
              e.preventDefault();
              setPending(true);
              try {
                await api<Operation>("/workers", {
                  name,
                  profile,
                  revision_id: latest?.id,
                  target_id: "local-docker",
                });
                setCreating(false);
                setName("");
                await client.invalidateQueries({ queryKey: ["workers"] });
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setPending(false);
              }
            }}
          >
            <div className="panel-heading">
              <h2>Create worker</h2>
              <button
                type="button"
                className="icon-button"
                onClick={() => setCreating(false)}
                aria-label="Close"
              >
                <X size={18} />
              </button>
            </div>
            <p>
              A new instance uses an immutable configuration snapshot and is
              created stopped.
            </p>
            <label>
              Instance name
              <input
                required
                pattern="[a-z][a-z0-9-]{2,59}"
                placeholder="factory-simple-02"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label>
              Worker profile
              <select
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
              >
                {Object.keys(config.data?.profiles ?? {}).map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
            <label>
              Revision
              <input readOnly value={latest?.id ?? "Loading…"} />
            </label>
            <Failure error={error} />
            <button className="primary" disabled={pending || !latest}>
              Create stopped worker
            </button>
          </form>
        </div>
      )}
      {configWorker && (
        <div className="modal-backdrop">
          <form
            className="modal"
            onSubmit={async (event) => {
              event.preventDefault();
              setPending(true);
              setError("");
              try {
                await api("/workers/" + configWorker.id + "/actions", {
                  action: "configure",
                  revision_id: applyRevision,
                });
                setConfigWorker(null);
                await client.invalidateQueries({ queryKey: ["workers"] });
              } catch (error) {
                setError((error as Error).message);
              } finally {
                setPending(false);
              }
            }}
          >
            <div className="panel-heading">
              <h2>Apply configuration</h2>
              <button
                type="button"
                className="icon-button"
                onClick={() => setConfigWorker(null)}
                aria-label="Close"
              >
                <X size={18} />
              </button>
            </div>
            <p>
              Replace the stopped runtime for {configWorker.name} with the
              selected snapshot. It stays stopped until explicitly started.
              Choosing an older revision restores that configuration.
            </p>
            <label>
              Configuration revision
              <select
                value={applyRevision}
                onChange={(event) => setApplyRevision(event.target.value)}
              >
                {revisions.data?.map((revision) => (
                  <option key={revision.id} value={revision.id}>
                    {revision.id.slice(0, 8)} · {revision.summary}
                  </option>
                ))}
              </select>
            </label>
            <Failure error={error} />
            <button className="primary" disabled={pending}>
              Apply to stopped worker
            </button>
          </form>
        </div>
      )}
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
            <pre>{logs || "No log output captured."}</pre>
          </div>
        </div>
      )}
    </>
  );
}
