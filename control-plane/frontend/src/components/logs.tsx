import { useState } from "react";

export function LogViewToggle({
  raw,
  onChange,
}: {
  raw: boolean;
  onChange: (raw: boolean) => void;
}) {
  return (
    <div className="actions log-view-toggle" role="group" aria-label="Log view">
      <button
        className="secondary"
        aria-pressed={!raw}
        onClick={() => onChange(false)}
      >
        Parsed
      </button>
      <button
        className="secondary"
        aria-pressed={raw}
        onClick={() => onChange(true)}
      >
        Raw
      </button>
    </div>
  );
}

export function RuntimeLogView({ content }: { content: string }) {
  const [raw, setRaw] = useState(false);
  const rows = content
    .split("\n")
    .filter((line) => line.length)
    .map((line) => {
      try {
        const value = JSON.parse(line);
        if (value && typeof value === "object" && !Array.isArray(value))
          return {
            timestamp: String(value.timestamp ?? value.time ?? "—"),
            level: String(value.level ?? "—"),
            message: String(value.message ?? line),
            stage: String(value.stage ?? "—"),
          };
      } catch {
        /* Preserve text and traceback lines as plain messages. */
      }
      return { timestamp: "—", level: "—", stage: "—", message: line };
    });
  return (
    <>
      <div className="toolbar">
        <LogViewToggle raw={raw} onChange={setRaw} />
        <span className="muted">Redacted runtime output</span>
      </div>
      {raw ? (
        <pre aria-label="Raw runtime logs">
          {content || "No log output captured."}
        </pre>
      ) : !rows.length ? (
        <p>No log output captured.</p>
      ) : (
        <div className="table-scroll">
          <table className="structured-logs">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Level</th>
                <th>Stage</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  <td className="mono">{row.timestamp}</td>
                  <td>
                    <span className={"log-level " + row.level.toLowerCase()}>
                      {row.level}
                    </span>
                  </td>
                  <td>{row.stage}</td>
                  <td className="log-message">{row.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
