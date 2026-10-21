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

export function parseLogMessage(
  message: unknown,
): Record<string, unknown> | null {
  if (typeof message !== "string") return null;
  try {
    const value: unknown = JSON.parse(message);
    return value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      typeof (value as Record<string, unknown>).message === "string"
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export function logLevel(value: unknown, fallback = "INFO") {
  const level = typeof value === "string" ? value.toUpperCase() : "";
  if (level === "WARN") return "WARNING";
  if (level === "FATAL") return "CRITICAL";
  return ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].includes(level)
    ? level
    : fallback;
}

export function RuntimeLogView({ content }: { content: string }) {
  const [raw, setRaw] = useState(false);
  const rows = content
    .split("\n")
    .filter((line) => line.length)
    .map((line) => {
      const value = parseLogMessage(line);
      if (value)
        return {
          timestamp: String(value.timestamp ?? value.time ?? "—"),
          level: logLevel(value.level, "—"),
          message: String(value.message),
          stage: String(value.stage ?? "—"),
        };
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
