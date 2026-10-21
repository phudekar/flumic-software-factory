import { useQuery } from "@tanstack/react-query";
import { Clock3, Search, Workflow } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import type { FactoryEvent, Run, Usage } from "../api";
import { api } from "../api";
import {
  date,
  count,
  money,
  Empty,
  Failure,
  PageTitle,
  RunTable,
  Section,
  Status,
} from "../components/ui";
import { useData } from "../hooks";
import { Calls } from "./history";

export function RunsPage() {
  const runs = useData<{ items: Run[]; warnings: string[] }>("runs", "/runs");
  const [search, setSearch] = useState("");
  return (
    <>
      <PageTitle
        eyebrow="ORCHESTRATION"
        title="Workflow runs"
        description="Execution status comes from Hatchet. Each retry keeps its own history."
      />
      <div className="toolbar">
        <label className="search">
          <Search size={15} />
          <input
            placeholder="Filter workflows…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <span className="tag">Latest 50 runs</span>
      </div>
      {runs.data?.warnings.map((x) => (
        <div className="notice" key={x}>
          {x}
        </div>
      ))}
      <Failure error={runs.error} />
      <Section title="Execution history">
        <RunTable
          items={
            runs.data?.items.filter((x) =>
              (x.name + " " + x.workflow)
                .toLowerCase()
                .includes(search.toLowerCase()),
            ) ?? []
          }
          loading={runs.isPending}
        />
      </Section>
    </>
  );
}

export function RunPage() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const selected = params.get("task") ?? "";
  const attempt = params.get("attempt") ?? "";
  const stage = selected ? "" : (params.get("stage") ?? "");
  const filter = new URLSearchParams({ run_id: id ?? "" });
  if (selected) filter.set("engine_task_id", selected);
  if (stage) filter.set("stage", stage);
  if (attempt) filter.set("attempt_id", attempt);
  if (params.get("worker")) filter.set("worker_id", params.get("worker")!);
  const query = filter.toString();
  const events = useData<FactoryEvent[]>(
    "events",
    "/events?" + query + "&limit=500",
  );
  const allAttempts = useData<FactoryEvent[]>(
    "attempts",
    "/events?run_id=" +
      encodeURIComponent(id ?? "") +
      "&kind=attempt.started&limit=500",
  );
  const engine = useData<{
    run: Run;
    tasks: { id: string; name: string; status: string }[];
  }>("run", "/runs/" + id);
  const tab =
    params.get("tab") === "logs"
      ? "logs"
      : params.get("tab") === "model-calls"
        ? "model calls"
        : "activity";
  function selectStage(task: string) {
    const next = new URLSearchParams(params);
    for (const key of ["task", "stage", "attempt", "worker"]) next.delete(key);
    if (task.startsWith("stage:")) next.set("stage", task.slice(6));
    else if (task) next.set("task", task);
    setParams(next);
  }
  function logsLink(event: FactoryEvent) {
    const next = new URLSearchParams({ tab: "logs", worker: event.worker_id });
    if (event.attempt_id) next.set("attempt", event.attempt_id);
    if (event.payload.engine_task_run_id)
      next.set("task", String(event.payload.engine_task_run_id));
    else if (event.stage) next.set("stage", event.stage);
    return "?" + next;
  }
  const uncorrelatedStages = [
    ...new Set(
      (allAttempts.data ?? [])
        .filter((e) => !e.payload.engine_task_run_id && e.stage)
        .map((e) => e.stage!),
    ),
  ];
  const list = events.data ?? [];
  const attempts = list.filter(
    (e) => e.kind === "attempt.started" || e.kind === "attempt.completed",
  );
  return (
    <>
      <Link className="text-link" to="/runs">
        ← All workflow runs
      </Link>
      <PageTitle
        eyebrow="WORKFLOW DETAIL"
        title={engine.data?.run?.name || "Run " + id?.slice(0, 12)}
        description="Inspect stage attempts, logs, and model exchanges without losing execution context."
      />
      <div className="run-info">
        <span className="tag mono">{id}</span>
        {engine.data?.run?.status && <Status value={engine.data.run.status} />}
        <span className="tag">
          <Clock3 size={13} />
          {attempts.length} recorded stage events
        </span>
      </div>
      <Failure error={engine.error || events.error} />
      {!!engine.data?.tasks.length && (
        <div className="stage-flow">
          {engine.data.tasks.map((task) => (
            <div className="stage-card" key={task.id}>
              <button
                className={selected === task.id ? "stage selected" : "stage"}
                onClick={() => selectStage(task.id)}
              >
                <Workflow size={15} />
                <strong>{task.name}</strong>
                <Status value={task.status} />
              </button>
              <Link
                className="text-link stage-logs-link"
                aria-label={"View logs for " + task.name}
                to={"?" + new URLSearchParams({ task: task.id, tab: "logs" })}
              >
                View logs →
              </Link>
            </div>
          ))}
        </div>
      )}
      <div className="toolbar">
        <button className="secondary" onClick={() => selectStage("")}>
          All stages and attempts
        </button>
        <label>
          Stage
          <select
            aria-label="Stage filter"
            value={selected || (stage ? "stage:" + stage : "")}
            onChange={(e) => selectStage(e.target.value)}
          >
            <option value="">All stages</option>
            {selected &&
              !engine.data?.tasks.some((task) => task.id === selected) && (
                <option value={selected}>Selected stage · {selected}</option>
              )}
            {engine.data?.tasks.map((task) => (
              <option key={task.id} value={task.id}>
                {task.name}
              </option>
            ))}
            {uncorrelatedStages.map((name) => (
              <option key={name} value={"stage:" + name}>
                {name} (worker events)
              </option>
            ))}
            {stage && !uncorrelatedStages.includes(stage) && (
              <option value={"stage:" + stage}>{stage} (worker events)</option>
            )}
          </select>
        </label>
        <label>
          Attempt
          <select
            aria-label="Stage attempt"
            value={attempt}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              if (e.target.value) next.set("attempt", e.target.value);
              else next.delete("attempt");
              setParams(next);
            }}
          >
            <option value="">All attempts</option>
            {(allAttempts.data ?? [])
              .filter(
                (e) =>
                  (!selected || e.payload.engine_task_run_id === selected) &&
                  (!stage || e.stage === stage),
              )
              .map((e) => (
                <option key={e.id} value={e.attempt_id ?? ""}>
                  {e.stage} · {e.attempt_id?.slice(0, 8)} ·{" "}
                  {date(e.occurred_at)}
                </option>
              ))}
          </select>
        </label>
        {params.get("worker") && (
          <Link className="text-link" to={"/workers/" + params.get("worker")}>
            Worker detail →
          </Link>
        )}
      </div>
      <ScopedUsage query={query} />
      <div className="tabs">
        {["activity", "logs", "model calls"].map((x) => (
          <button
            className={tab === x ? "active" : ""}
            key={x}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("tab", x.replaceAll(" ", "-"));
              setParams(next);
            }}
          >
            {x}
          </button>
        ))}
      </div>
      {tab === "activity" && (
        <Section title="Stage attempts">
          {!attempts.length ? (
            <Empty
              title="No instrumented stage events yet"
              detail="Hatchet task state is shown above. Detailed history starts when newly instrumented workers execute a task."
            />
          ) : (
            <div className="timeline">
              {attempts.map((e) => (
                <div key={e.id}>
                  <i />
                  <div>
                    <strong>{e.stage}</strong>
                    <small>{e.attempt_id}</small>
                  </div>
                  <Status value={String(e.payload.outcome ?? "started")} />
                  <time>{date(e.occurred_at)}</time>
                  <Link className="text-link" to={logsLink(e)}>
                    View logs →
                  </Link>
                </div>
              ))}
            </div>
          )}
        </Section>
      )}
      {tab === "logs" && (
        <Logs events={list} task={selected} attempt={attempt} />
      )}
      {tab === "model calls" && (
        <Calls events={list.filter((x) => x.kind === "model.completed")} />
      )}
      {list.length === 500 && (
        <div className="notice">
          Showing the latest 500 matching events. Usage totals query up to
          10,000 matching calls separately.
        </div>
      )}
    </>
  );
}

export function ScopedUsage({ query }: { query: string }) {
  const data = useData<Usage>("scoped-usage", "/usage?" + query);
  const usage = data.data;
  return (
    <Section title="Model usage for this selection">
      <Failure error={data.error} />
      <div className="coverage">
        <div>
          <span>Model calls</span>
          <strong>{count(usage?.calls)}</strong>
        </div>
        <div>
          <span>Input / output tokens</span>
          <strong>
            {count(usage?.input_tokens)} / {count(usage?.output_tokens)}
          </strong>
        </div>
        <div>
          <span>Estimated USD subtotal</span>
          <strong>{money(usage?.estimated_usd)}</strong>
        </div>
        <div>
          <span>Reported USD (separate)</span>
          <strong>{money(usage?.reported_usd)}</strong>
        </div>
        <div>
          <span>Unpriced / missing usage</span>
          <strong>
            {count(usage?.unpriced_calls)} / {count(usage?.missing_usage_calls)}
          </strong>
        </div>
      </div>
      {usage?.truncated && (
        <div className="notice">
          Limited to the latest 10,000 matching calls.
        </div>
      )}
    </Section>
  );
}

function ExchangeLog({ event }: { event: FactoryEvent }) {
  const [open, setOpen] = useState(false);
  const transcript = useQuery<Record<string, unknown>>({
    queryKey: ["conversation", event.id],
    queryFn: () => api("/model-calls/" + event.id + "/conversation"),
    enabled: open,
  });
  const stringify = (value: unknown) =>
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div className="exchange-log">
      <button
        className="secondary"
        disabled={!event.payload.conversation_available}
        onClick={() => setOpen(!open)}
      >
        {open ? "Collapse exchange" : "Inspect worker prompt and model output"}
      </button>
      {open && (
        <div className="exchange-content">
          <Failure error={transcript.error} />
          <div className="exchange-input">
            <h3>Worker input prompt</h3>
            <small>
              {transcript.data?.request_at
                ? date(Number(transcript.data.request_at))
                : "Request timestamp unavailable for this capture"}
            </small>
            <pre>{stringify(transcript.data?.request) ?? "Loading…"}</pre>
          </div>
          <div className="exchange-output">
            <h3>Model output</h3>
            <small>
              {transcript.data?.response_at
                ? date(Number(transcript.data.response_at))
                : "Response timestamp unavailable for this capture"}
            </small>
            <pre>{stringify(transcript.data?.response) ?? "Not captured"}</pre>
          </div>
          <small>
            Redacted · {String(transcript.data?.coverage ?? "Loading capture…")}
          </small>
        </div>
      )}
    </div>
  );
}

export function Logs({
  events,
  task,
  attempt = "",
}: {
  events: FactoryEvent[];
  task: string;
  attempt?: string;
}) {
  const [level, setLevel] = useState("");
  const [kind, setKind] = useState("");
  const logs = useQuery<{ items: Record<string, unknown>[] }>({
    queryKey: ["engine-logs", task],
    queryFn: () => api("/engine/tasks/" + task + "/logs"),
    enabled: !!task && !attempt,
  });
  const rows = events
    .filter((e) => e.kind === "log" || e.kind === "model.completed")
    .map((e) => ({
      id: e.id,
      at: date(e.occurred_at),
      order: e.occurred_at,
      level: String(
        e.payload.level ?? (e.payload.outcome === "failed" ? "ERROR" : "INFO"),
      ).toUpperCase(),
      kind: e.kind === "model.completed" ? "Model exchange" : "Worker log",
      stage: e.stage ?? "—",
      worker: e.worker_id,
      message: String(
        e.payload.message ??
          (e.payload.model ?? "Unknown model") +
            " · " +
            (e.payload.outcome ?? "unknown"),
      ),
      event: e.kind === "model.completed" ? e : null,
    }));
  for (const item of (!attempt ? logs.data?.items : []) ?? [])
    rows.push({
      id: "engine-" + rows.length,
      at: date(String(item.createdAt ?? "")),
      order: Date.parse(String(item.createdAt ?? "")) / 1000,
      level: String(item.level ?? "INFO").toUpperCase(),
      kind: "Engine log",
      stage: "Selected task",
      worker: "Hatchet",
      message: String(item.message ?? ""),
      event: null,
    });
  const filtered = rows
    .filter((r) => (!level || r.level === level) && (!kind || r.kind === kind))
    .sort((a, b) => a.order - b.order);
  return (
    <Section
      title="Stage logs and model exchanges"
      aside={<span className="tag">Redacted · latest page</span>}
    >
      <div className="toolbar">
        <label>
          Severity
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">All levels</option>
            {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => (
              <option key={l}>{l}</option>
            ))}
          </select>
        </label>
        <label>
          Entry type
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All entries</option>
            {["Worker log", "Model exchange", "Engine log"].map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>
      </div>
      <Failure error={logs.error} />
      {attempt && (
        <div className="notice">
          Showing correlated worker events for this attempt. Hatchet logs are
          task-scoped and cannot be safely separated by retry here.
        </div>
      )}
      {!filtered.length ? (
        <Empty
          title="No matching log entries"
          detail="Select a stage or change the filters. Historical input and output require captured conversations."
        />
      ) : (
        <div className="table-scroll">
          <table className="structured-logs">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Level</th>
                <th>Type / stage / worker</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.id}>
                  <td className="mono">
                    <time>{r.at}</time>
                  </td>
                  <td>
                    <span className={"log-level " + r.level.toLowerCase()}>
                      {r.level}
                    </span>
                  </td>
                  <td>
                    <strong>{r.kind}</strong>
                    <small>{r.stage}</small>
                    <small>
                      {r.worker === "Hatchet" ? (
                        r.worker
                      ) : (
                        <Link className="text-link" to={"/workers/" + r.worker}>
                          {r.worker.slice(0, 8)}
                        </Link>
                      )}
                    </small>
                  </td>
                  <td className="log-message">
                    {r.message}
                    {r.event && <ExchangeLog event={r.event} />}
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
