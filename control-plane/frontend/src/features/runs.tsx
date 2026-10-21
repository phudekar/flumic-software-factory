import { useQuery } from "@tanstack/react-query";
import { Clock3, Search, Workflow } from "lucide-react";
import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import type { FactoryEvent, Run } from "../api";
import { api } from "../api";
import {
  date,
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
  const events = useData<FactoryEvent[]>(
    "events",
    "/events?run_id=" + encodeURIComponent(id ?? ""),
  );
  const engine = useData<{
    run: Run;
    tasks: { id: string; name: string; status: string }[];
  }>("run", "/runs/" + id);
  const [tab, setTab] = useState("activity");
  const [selected, setSelected] = useState("");
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
            <React.Fragment key={task.id}>
              <button
                className={selected === task.id ? "stage selected" : "stage"}
                onClick={() => setSelected(task.id)}
              >
                <Workflow size={15} />
                <strong>{task.name}</strong>
                <Status value={task.status} />
              </button>
            </React.Fragment>
          ))}
        </div>
      )}
      <div className="tabs">
        {["activity", "logs", "model calls"].map((x) => (
          <button
            className={tab === x ? "active" : ""}
            key={x}
            onClick={() => setTab(x)}
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
                </div>
              ))}
            </div>
          )}
        </Section>
      )}
      {tab === "logs" && (
        <Logs events={list.filter((x) => x.kind === "log")} task={selected} />
      )}
      {tab === "model calls" && (
        <Calls events={list.filter((x) => x.kind === "model.completed")} />
      )}
    </>
  );
}

export function Logs({
  events,
  task,
}: {
  events: FactoryEvent[];
  task: string;
}) {
  const logs = useQuery<{ items: Record<string, unknown>[] }>({
    queryKey: ["engine-logs", task],
    queryFn: () => api("/engine/tasks/" + task + "/logs"),
    enabled: !!task,
  });
  return (
    <Section
      title="Stage logs"
      aside={<span className="tag">Redacted · latest page</span>}
    >
      {!events.length && !logs.data?.items.length ? (
        <Empty
          title="No stage logs available"
          detail="Select a stage for Hatchet logs. Worker-correlated logs appear after instrumentation is deployed."
        />
      ) : (
        <pre className="log-view">
          {events
            .slice()
            .reverse()
            .map(
              (e) =>
                date(e.occurred_at) +
                " " +
                String(e.payload.level) +
                " [" +
                e.stage +
                "] " +
                String(e.payload.message),
            )
            .join("\n")}
          {logs.data?.items
            .map(
              (item) =>
                "\n" + String(item.createdAt) + " " + String(item.message),
            )
            .join("")}
        </pre>
      )}
      <Failure error={logs.error} />
    </Section>
  );
}
