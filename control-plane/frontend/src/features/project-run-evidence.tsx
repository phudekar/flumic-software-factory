import { useState } from "react";
import type { FactoryEvent } from "../api";
import { Empty, Failure, Status } from "../components/ui";
import { localTimestamp } from "../components/project-format";
import { ProjectActivity } from "../components/project-activity";
import { useProjectData } from "../project-context";
import type { ModelAccountingEvent } from "../workflow-api";
import { ProjectModelCalls } from "./project-model-calls";
import { ProjectAccounting } from "./usage";
import { useSearchParams } from "react-router-dom";

export function RunEvidence({
  runId,
  stage,
  execution,
  mode,
}: {
  runId: string;
  stage?: string;
  execution?: string;
  mode: "result" | "logs" | "models";
}) {
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const filters = new URLSearchParams({ run_id: runId });
  if (stage) filters.set("stage", stage);
  if (execution) filters.set("execution_id", execution);
  // Old stage links may contain extra model/history filters. Never carry these
  // into run-wide views or the selected attempt's Result evidence.
  if (stage && mode !== "result")
    for (const [key, apiKey] of [
      ["worker", "worker_id"],
      ["provider", "provider"],
      ["model", "model"],
    ]) {
      const value = params.get(key);
      if (value && (mode === "models" || key === "worker"))
        filters.set(apiKey, value);
    }
  const query = filters.toString();
  const events = useProjectData<FactoryEvent[]>(
    "/events?" + query + "&limit=200",
  );
  const modelEvents =
    events.data?.filter(
      (e): e is ModelAccountingEvent => e.kind === "model.completed",
    ) ?? [];
  const updateFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    setParams(next, { replace: true });
  };
  return (
    <div className="run-evidence">
      <Failure error={events.error} />
      {stage && mode !== "result" && (
        <div className="run-evidence-filters">
          <label>
            Worker ID
            <input
              value={params.get("worker") ?? ""}
              placeholder="All workers"
              onChange={(e) => updateFilter("worker", e.target.value)}
            />
          </label>
          {mode === "models" && (
            <label>
              Model
              <input
                value={params.get("model") ?? ""}
                placeholder="All models"
                onChange={(e) => updateFilter("model", e.target.value)}
              />
            </label>
          )}
          {["worker", "provider", "model"].some((k) => params.has(k)) && (
            <button
              onClick={() => {
                const next = new URLSearchParams(params);
                ["worker", "provider", "model"].forEach((k) => next.delete(k));
                setParams(next, { replace: true });
              }}
            >
              Clear history filters
            </button>
          )}
        </div>
      )}
      {events.isPending ? (
        <p role="status">Loading captured evidence…</p>
      ) : !events.data ? null : (
        <>
          {mode === "result" ? (
            <ResultEvidence
              events={events.data}
              onModels={() => {
                const next = new URLSearchParams(params);
                next.set("detail", "models");
                setParams(next);
              }}
            />
          ) : mode === "models" ? (
            <>
              <ProjectAccounting query={query} />
              <ProjectModelCalls events={modelEvents} />
            </>
          ) : (
            <>
              <div className="run-log-toolbar">
                <div>
                  <h3>{stage ? "Stage activity" : "Run activity"}</h3>
                  <p>
                    Latest 200 retained events ·{" "}
                    {stage
                      ? "selected stage and attempt"
                      : "all stages and attempts"}
                  </p>
                </div>
                <label>
                  Search activity
                  <input
                    type="search"
                    value={search}
                    placeholder="Message, stage or level"
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </label>
              </div>
              <p className="run-evidence-note">
                Captured worker logs only. Retention can leave gaps; these
                entries do not establish complete engine history or model spend.
              </p>
              <ProjectActivity
                events={events.data.filter(
                  (e) =>
                    !search ||
                    [
                      e.stage,
                      e.payload.message,
                      e.payload.event,
                      e.payload.level,
                    ].some(
                      (v) =>
                        typeof v === "string" &&
                        v.toLowerCase().includes(search.toLowerCase()),
                    ),
                )}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}

function ResultEvidence({
  events,
  onModels,
}: {
  events: FactoryEvent[];
  onModels: () => void;
}) {
  const start = events
    .filter((e) => e.kind === "attempt.started")
    .sort((a, b) => a.occurred_at - b.occurred_at)[0];
  const finish = events
    .filter((e) => e.kind === "attempt.completed")
    .sort((a, b) => b.occurred_at - a.occurred_at)[0];
  const duration = finish?.payload.duration_seconds;
  const logs = events
    .filter(
      (e) =>
        e.kind === "log" &&
        typeof e.payload.message === "string" &&
        !["Operation started", "Operation finished"].includes(
          e.payload.message,
        ),
    )
    .sort((a, b) => a.occurred_at - b.occurred_at);
  return (
    <div className="run-result">
      <div className="run-result-timing">
        <div>
          <span>Captured start</span>
          <time>{localTimestamp(start?.occurred_at)}</time>
        </div>
        <div>
          <span>Captured finish</span>
          <time>{localTimestamp(finish?.occurred_at)}</time>
        </div>
        <div>
          <span>Reported duration</span>
          <strong>
            {typeof duration === "number" && Number.isFinite(duration)
              ? new Intl.NumberFormat(undefined, {
                  maximumFractionDigits: 2,
                }).format(duration) + " s"
              : "Unknown"}
          </strong>
        </div>
      </div>
      {typeof finish?.payload.outcome === "string" && (
        <div className="run-recorded-outcome">
          <span>Recorded operation outcome</span>
          <Status value={finish.payload.outcome} />
          <small>
            This operation receipt is separate from the issue or pull request
            outcome.
          </small>
        </div>
      )}
      <section className="run-result-section">
        <h3>Captured activity</h3>
        {logs.length ? (
          <ol className="run-result-messages">
            {logs.slice(-8).map((e) => (
              <li key={e.id}>
                <time>{localTimestamp(e.occurred_at)}</time>
                <p>
                  {String(e.payload.message).slice(0, 2000)}
                  {String(e.payload.message).length > 2000
                    ? "… (see Logs for captured details)"
                    : ""}
                </p>
              </li>
            ))}
          </ol>
        ) : (
          <Empty
            title="No retained activity summary"
            detail="No descriptive worker messages are available for this attempt. Use Logs and Model calls to inspect other retained evidence."
          />
        )}
        {logs.length > 8 && (
          <small>
            Showing the latest eight descriptive messages. Open Logs for other
            retained entries.
          </small>
        )}
      </section>
      <div className="run-output-availability">
        <h3>Action output &amp; model responses</h3>
        <p>
          Structured action output is not exposed by this run API. Captured
          model responses are available separately when retained; they are not
          the canonical action result.
        </p>
        <button onClick={onModels}>Inspect stage model calls →</button>
      </div>
    </div>
  );
}
