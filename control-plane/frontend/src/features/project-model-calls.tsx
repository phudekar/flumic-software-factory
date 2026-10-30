import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { count, Empty, Failure, money, Section } from "../components/ui";
import {
  localTimestamp,
  relativeTimestamp,
} from "../components/project-format";
import { projectPath, useProjectId } from "../project-context";
import type { ModelAccountingEvent } from "../workflow-api";
import "./project-model-calls.css";

// Strings may themselves contain structured JSON, as with CLI model responses.
function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}
function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}
function text(value: unknown, fallback = "Unknown") {
  if (value === undefined || value === null || value === "") return fallback;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
const contextFields = new Set([
  "repository_source_paths",
  "source_files",
  "current_source_files",
  "changed_files",
  "source_inventory",
  "file_contents",
  "repository_context",
  "repository_files",
  "issue_comments",
  "diff",
  "diffs",
  "git_diff",
  "context",
]);
function CollapsedContext({ value, depth }: { value: unknown; depth: number }) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Expand captured context</summary>
      {open && <CapturedValue value={value} depth={depth} compactContext />}
    </details>
  );
}
function CapturedValue({
  value,
  depth = 0,
  compactContext = false,
}: {
  value: unknown;
  depth?: number;
  compactContext?: boolean;
}) {
  const data = parsed(value);
  if (depth > 12) return <pre>{JSON.stringify(data, null, 2)}</pre>;
  if (data === undefined || data === null) return <p>No captured content.</p>;
  if (Array.isArray(data))
    return data.length ? (
      <ol className="project-exchange-list">
        {data.map((item, i) => (
          <li key={i}>
            <CapturedValue
              value={item}
              depth={depth + 1}
              compactContext={compactContext}
            />
          </li>
        ))}
      </ol>
    ) : (
      <p>Empty list</p>
    );
  const fields = record(data);
  if (fields)
    return Object.keys(fields).length ? (
      <dl className="project-exchange-fields">
        {Object.entries(fields).map(([key, item]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>
              {compactContext && contextFields.has(key) ? (
                <CollapsedContext value={item} depth={depth + 1} />
              ) : (
                <CapturedValue
                  value={item}
                  depth={depth + 1}
                  compactContext={compactContext}
                />
              )}
            </dd>
          </div>
        ))}
      </dl>
    ) : (
      <p>Empty object</p>
    );
  return <p className="project-exchange-text">{String(data)}</p>;
}
function RequestContent({ value }: { value: unknown }) {
  const data = parsed(value),
    fields = record(data);
  const messages = Array.isArray(data)
    ? data
    : Array.isArray(fields?.messages)
      ? fields.messages
      : null;
  if (!messages) return <CapturedValue value={value} compactContext />;
  const context = messages.filter((m) =>
    ["system", "developer"].includes(text(record(m)?.role, "")),
  );
  const conversation = messages.filter((m) => !context.includes(m));
  const renderMessage = (message: unknown, index: number) => {
    const item = record(message);
    return (
      <article className="project-exchange-message" key={index}>
        <h4>{text(item?.role, "Message")}</h4>
        <CapturedValue
          value={item && "content" in item ? item.content : message}
          compactContext
        />
      </article>
    );
  };
  return (
    <>
      {conversation.map(renderMessage)}
      {context.length > 0 && (
        <details>
          <summary>System guidance &amp; supplied context</summary>
          {context.map(renderMessage)}
        </details>
      )}
      {fields && Object.keys(fields).some((k) => k !== "messages") && (
        <details>
          <summary>Other request fields</summary>
          <CapturedValue
            value={Object.fromEntries(
              Object.entries(fields).filter(([key]) => key !== "messages"),
            )}
          />
        </details>
      )}
    </>
  );
}
function Timestamp({ value }: { value: unknown }) {
  const time =
    typeof value === "number" || typeof value === "string" ? value : null;
  return (
    <time className="project-exchange-time" title={localTimestamp(time)}>
      {localTimestamp(time)}
      <small>{relativeTimestamp(time)}</small>
    </time>
  );
}

export function ProjectModelCalls({
  events,
}: {
  events: ModelAccountingEvent[];
}) {
  const project = useProjectId();
  // Changing projects remounts the selection and aborts any old transcript query.
  return <ModelCallList key={project} events={events} project={project} />;
}
function ModelCallList({
  events,
  project,
}: {
  events: ModelAccountingEvent[];
  project: string;
}) {
  const [search, setSearch] = useState(""),
    [provider, setProvider] = useState(""),
    [capture, setCapture] = useState("");
  const [selected, setSelected] = useState<{
    event: ModelAccountingEvent;
    opener: HTMLButtonElement;
  } | null>(null);
  const visible = events.filter(
    (e) =>
      (!provider || e.payload.provider === provider) &&
      (!capture ||
        Boolean(e.payload.conversation_available) ===
          (capture === "available")) &&
      [
        e.id,
        e.stage,
        e.payload.provider,
        e.payload.model,
        e.payload.outcome,
        e.worker_id,
      ].some((value) =>
        text(value, "").toLowerCase().includes(search.toLowerCase()),
      ),
  );
  // A same-project history refresh/navigation must not keep a removed call open.
  const active =
    selected && events.some((e) => e.id === selected.event.id)
      ? selected
      : null;
  return (
    <Section
      title="Project model calls"
      aside={
        <span className="tag">
          {visible.length} of {events.length} records
        </span>
      }
    >
      <div className="model-call-body">
        <p className="model-call-notice">
          Captured content expires after seven days. Metadata-only or disabled
          capture may omit exchanges. CLI internal conversations may be
          unavailable. Parent summaries are excluded from aggregate totals. A
          model-call result does not establish workflow or business success.
        </p>
        <div className="model-call-toolbar">
          <label>
            Search model calls
            <input
              type="search"
              value={search}
              placeholder="Model, provider, stage or ID"
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <label>
            Provider
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              <option value="">All providers</option>
              {Array.from(
                new Set(
                  events
                    .map((e) => e.payload.provider)
                    .filter((p): p is string => Boolean(p)),
                ),
              )
                .sort()
                .map((p) => (
                  <option key={p}>{p}</option>
                ))}
            </select>
          </label>
          <label>
            Exchange capture
            <select
              value={capture}
              onChange={(e) => setCapture(e.target.value)}
            >
              <option value="">All capture states</option>
              <option value="available">Exchange available</option>
              <option value="missing">No retained exchange</option>
            </select>
          </label>
          {(search || provider || capture) && (
            <button
              onClick={() => {
                setSearch("");
                setProvider("");
                setCapture("");
              }}
            >
              Clear filters
            </button>
          )}
        </div>
        {visible.length ? (
          <div className="model-call-table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Provider / model</th>
                  <th scope="col">Stage / time</th>
                  <th scope="col">Tokens in / out</th>
                  <th scope="col">Estimated cost</th>
                  <th scope="col">Reported cost</th>
                  <th scope="col">Result</th>
                  <th scope="col">Exchange</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((e) => (
                  <tr key={e.id}>
                    <td>
                      <strong>{text(e.payload.model, "Unknown model")}</strong>
                      <small>
                        {text(e.payload.provider, "Unknown provider")}
                      </small>
                      {e.payload.accounting_leaf === false && (
                        <small>Parent summary — excluded from totals</small>
                      )}
                    </td>
                    <td>
                      {e.stage ?? "Unknown stage"}
                      <small>
                        <Timestamp value={e.occurred_at} />
                      </small>
                    </td>
                    <td>
                      {count(e.payload.input_tokens)} /{" "}
                      {count(e.payload.output_tokens)}
                    </td>
                    <td>{money(e.payload.estimated_usd)}</td>
                    <td>{money(e.payload.reported_cost_usd)}</td>
                    <td>{text(e.payload.outcome, "Unknown")}</td>
                    <td>
                      <button
                        aria-label={"View exchange " + e.id}
                        disabled={!e.payload.conversation_available}
                        onClick={(ev) =>
                          setSelected({ event: e, opener: ev.currentTarget })
                        }
                      >
                        View exchange
                      </button>
                      {!e.payload.conversation_available && (
                        <small>No retained exchange</small>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title={
              events.length
                ? "No matching model calls"
                : "No captured model calls"
            }
            detail={
              events.length
                ? "Change or clear these filters to see other records."
                : "No retained model records match these filters. Unknown capture is not evidence of no model usage."
            }
          />
        )}
        {active && (
          <ExchangeDialog
            key={active.event.id}
            project={project}
            event={active.event}
            opener={active.opener}
            close={() => setSelected(null)}
          />
        )}
      </div>
    </Section>
  );
}
function ExchangeDialog({
  project,
  event,
  opener,
  close,
}: {
  project: string;
  event: ModelAccountingEvent;
  opener: HTMLButtonElement;
  close: () => void;
}) {
  const responseHeading = useRef<HTMLHeadingElement>(null),
    requestHeading = useRef<HTMLHeadingElement>(null);
  const heading = useId(),
    panel = useRef<HTMLDivElement>(null),
    closeRef = useRef<HTMLButtonElement>(null);
  const transcript = useQuery<unknown>({
    queryKey: ["project", project, "conversation", event.id],
    queryFn: ({ signal }) =>
      api(
        projectPath(
          project,
          "/model-calls/" + encodeURIComponent(event.id) + "/conversation",
        ),
        undefined,
        { signal },
      ),
    refetchInterval: false,
  });
  useEffect(() => {
    closeRef.current?.focus();
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
      if (opener.isConnected) opener.focus();
    };
  }, [opener]);
  const [rawOpen, setRawOpen] = useState(false);
  const data = record(transcript.data);
  return createPortal(
    <div
      className="project-exchange-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        className="project-exchange-drawer"
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={heading}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            close();
          }
          if (e.key === "Tab") {
            const elements = Array.from(
              panel.current?.querySelectorAll<HTMLElement>(
                'button:not(:disabled), a[href], input, select, textarea, summary, [tabindex="0"]',
              ) ?? [],
            ).filter(
              (el) =>
                !el.closest("details:not([open])") || el.tagName === "SUMMARY",
            );
            const first = elements[0],
              last = elements[elements.length - 1];
            if (e.shiftKey && document.activeElement === first) {
              e.preventDefault();
              last?.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
              e.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <header className="project-exchange-header">
          <div>
            <span className="eyebrow">MODEL EXCHANGE</span>
            <h2 id={heading}>{text(event.payload.model, "Model exchange")}</h2>
            <p>
              {text(event.payload.provider, "Unknown provider")} ·{" "}
              {event.stage ?? "Unknown stage"} ·{" "}
              <Timestamp value={event.occurred_at} />
            </p>
          </div>
          <button ref={closeRef} onClick={close} aria-label="Close exchange">
            Close
          </button>
        </header>
        <p>
          Result: {text(event.payload.outcome, "Unknown")}. Model-call success
          does not imply workflow completion.
        </p>
        <div className="model-call-exchange-usage">
          <span>
            Input tokens <strong>{count(event.payload.input_tokens)}</strong>
          </span>
          <span>
            Output tokens <strong>{count(event.payload.output_tokens)}</strong>
          </span>
          <span>
            Estimated <strong>{money(event.payload.estimated_usd)}</strong>
          </span>
          <span>
            Reported <strong>{money(event.payload.reported_cost_usd)}</strong>
          </span>
        </div>
        {event.payload.accounting_leaf === false && (
          <p>Parent summary — excluded from totals</p>
        )}
        <p className="model-call-notice">
          Redacted captured content; retention and capture policies may leave
          gaps. Content expires after seven days.
        </p>
        {transcript.isPending && <p role="status">Loading exchange…</p>}
        <Failure error={transcript.error} />
        {transcript.isError && (
          <p>
            The exchange may be expired, unavailable, or inaccessible. Metadata
            remains available in the table.
          </p>
        )}
        {transcript.isSuccess && (
          <>
            <p>Capture coverage: {text(data?.coverage, "Unknown")}</p>
            <p>
              Request captured: <Timestamp value={data?.request_at} />
            </p>
            <p>
              Response captured: <Timestamp value={data?.response_at} />
            </p>
            <div className="project-exchange-jumps">
              <button
                type="button"
                onClick={() =>
                  requestHeading.current?.scrollIntoView({ block: "start" })
                }
              >
                View request
              </button>
              <button
                type="button"
                onClick={() =>
                  responseHeading.current?.scrollIntoView({ block: "start" })
                }
              >
                View response
              </button>
            </div>
            <h3 ref={requestHeading}>Request</h3>
            <RequestContent value={data?.request} />
            <h3 ref={responseHeading}>Response</h3>
            <div className="project-exchange-message">
              <h4>Assistant response</h4>
              <CapturedValue value={data?.response} />
            </div>
            {!data || (!("request" in data) && !("response" in data)) ? (
              <>
                <p>No structured request or response was captured.</p>
                <CapturedValue value={transcript.data} />
              </>
            ) : null}
            <details onToggle={(e) => setRawOpen(e.currentTarget.open)}>
              <summary>Complete original raw payload</summary>
              {rawOpen && <pre>{JSON.stringify(transcript.data, null, 2)}</pre>}
            </details>
          </>
        )}
        <details>
          <summary>Metadata and correlation IDs</summary>
          <pre>{JSON.stringify(event, null, 2)}</pre>
        </details>
      </div>
    </div>,
    document.body,
  );
}
