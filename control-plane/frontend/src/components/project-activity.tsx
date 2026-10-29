import type { FactoryEvent } from "../api";
import { Empty } from "./ui";
import { localTimestamp } from "./project-format";

export function ProjectActivity({ events }: { events: FactoryEvent[] }) {
  const logs = events.filter((event) => event.kind === "log").slice(0, 200);
  if (!logs.length)
    return (
      <Empty
        title="No captured logs"
        detail="No retained log records match this selection."
      />
    );
  let remaining = 65536;
  return (
    <div className="table-scroll">
      <table className="project-activity-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Stage / level</th>
            <th>Activity</th>
          </tr>
        </thead>
        <tbody>
          {logs.map((event) => {
            const original = JSON.stringify(event.payload, null, 2);
            const content = original.slice(0, Math.max(remaining, 0));
            remaining -= content.length;
            const message =
              typeof event.payload.message === "string"
                ? event.payload.message
                : typeof event.payload.event === "string"
                  ? event.payload.event
                  : "Captured log event";
            return (
              <tr key={event.id}>
                <td>
                  <time>{localTimestamp(event.occurred_at)}</time>
                </td>
                <td>
                  <strong>{event.stage ?? "Unspecified stage"}</strong>
                  <small>
                    {typeof event.payload.level === "string"
                      ? event.payload.level
                      : "Log"}
                  </small>
                </td>
                <td>
                  <p className="project-log-message">
                    {message.slice(0, 600)}
                    {message.length > 600 ? "…" : ""}
                  </p>
                  <details>
                    <summary>Captured event details</summary>
                    <pre>{content || "Preview limit reached."}</pre>
                    {content.length < original.length && (
                      <small>
                        Preview truncated. The page shows at most 65,536
                        captured payload characters.
                      </small>
                    )}
                  </details>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
