import { useQuery } from "@tanstack/react-query";
import { MessageSquareText, ShieldCheck, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import type { FactoryEvent } from "../api";
import { api } from "../api";
import {
  count,
  date,
  Empty,
  Failure,
  money,
  PageTitle,
  Section,
  Status,
} from "../components/ui";
import { useData } from "../hooks";

export function History() {
  const events = useData<FactoryEvent[]>(
    "calls",
    "/events?kind=model.completed",
  );
  return (
    <>
      <PageTitle
        eyebrow="MODEL OBSERVABILITY"
        title="Model history"
        description="Redacted requests and responses, linked to the task that produced them."
      />
      <div className="notice">
        <ShieldCheck size={16} />
        API exchanges and CLI prompts/results are captured going forward. CLI
        internal conversations may be unavailable. Content expires after seven
        days.
      </div>
      <Failure error={events.error} />
      <Calls events={events.data ?? []} />
    </>
  );
}

export function Calls({ events }: { events: FactoryEvent[] }) {
  const [selected, setSelected] = useState<FactoryEvent | null>(null);
  const transcript = useQuery<Record<string, unknown>>({
    queryKey: ["conversation", selected?.id],
    queryFn: () => api("/model-calls/" + selected?.id + "/conversation"),
    enabled: !!selected,
    refetchInterval: false,
  });
  return (
    <>
      <Section
        title="Model calls"
        aside={<span className="tag">{events.length} on this page</span>}
      >
        {!events.length ? (
          <Empty
            title="No model history captured yet"
            detail="New control-plane workers record redacted model exchanges. Existing OpenTelemetry metadata contains no historical prompts or responses."
          />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Model / tool</th>
                  <th>Stage</th>
                  <th>Tokens in / out</th>
                  <th>Estimated cost</th>
                  <th>Reported cost</th>
                  <th>Result</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td>
                      <strong>
                        {String(e.payload.model ?? "Unknown model")}
                      </strong>
                      <small>
                        {String(e.payload.agent ?? "api")} ·{" "}
                        {date(e.occurred_at)}
                      </small>
                    </td>
                    <td>
                      {e.payload.engine_workflow_run_id ? (
                        <Link
                          className="text-link"
                          to={
                            "/runs/" +
                            String(e.payload.engine_workflow_run_id) +
                            "?" +
                            new URLSearchParams({
                              task: String(e.payload.engine_task_run_id ?? ""),
                              attempt: e.attempt_id ?? "",
                              worker: e.worker_id,
                            })
                          }
                        >
                          {e.stage ?? "Stage"}
                        </Link>
                      ) : (
                        (e.stage ?? "Unscoped")
                      )}
                      <small>
                        <Link
                          className="text-link"
                          to={"/workers/" + e.worker_id}
                        >
                          Worker {e.worker_id.slice(0, 8)}
                        </Link>
                      </small>
                      {e.payload.accounting_leaf === false && (
                        <small>Parent summary · excluded from totals</small>
                      )}
                    </td>
                    <td className="mono">
                      {count(e.payload.input_tokens as number)} /{" "}
                      {count(e.payload.output_tokens as number)}
                    </td>
                    <td className="mono">
                      {money(e.payload.estimated_usd as number)}
                    </td>
                    <td className="mono">
                      {money(e.payload.reported_cost_usd as number)}
                    </td>
                    <td>
                      <Status value={String(e.payload.outcome ?? "unknown")} />
                    </td>
                    <td>
                      <button
                        className="secondary"
                        disabled={!e.payload.conversation_available}
                        onClick={() => setSelected(e)}
                      >
                        <MessageSquareText size={13} />
                        Exchange
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      {selected && (
        <div className="modal-backdrop">
          <div className="modal wide">
            <div className="panel-heading">
              <div>
                <h2>{String(selected.payload.model ?? "Model exchange")}</h2>
                <small>
                  {selected.stage} · {selected.attempt_id}
                </small>
              </div>
              <button
                className="icon-button"
                onClick={() => setSelected(null)}
                aria-label="Close conversation"
              >
                <X />
              </button>
            </div>
            <Failure error={transcript.error} />
            <span className="tag">
              {String(transcript.data?.coverage ?? "Loading capture…")}
            </span>
            <h3>Request</h3>
            <pre>{JSON.stringify(transcript.data?.request, null, 2)}</pre>
            <h3>Response</h3>
            <pre>
              {typeof transcript.data?.response === "string"
                ? transcript.data.response
                : JSON.stringify(transcript.data?.response, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </>
  );
}
