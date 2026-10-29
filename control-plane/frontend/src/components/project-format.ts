type Timestamp = number | string | null | undefined;
function asDate(value: Timestamp): Date | null {
  if (
    value == null ||
    value === "" ||
    (typeof value === "string" && !value.trim())
  )
    return null;
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date : null;
}
/** API numbers are Unix seconds; the browser supplies the viewer's locale/timezone. */
export function localTimestamp(value: Timestamp): string {
  const date = asDate(value);
  return date
    ? new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        timeZoneName: "short",
      }).format(date)
    : "Unknown";
}
export function relativeTimestamp(value: Timestamp, now = Date.now()): string {
  const date = asDate(value);
  if (!date || !Number.isFinite(now)) return "Unknown";
  const seconds = (date.getTime() - now) / 1000;
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31536000],
    ["month", 2592000],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];
  const [unit, divisor] =
    units.find(([, size]) => Math.abs(seconds) >= size) ??
    units[units.length - 1];
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    Math.round(seconds / divisor),
    unit,
  );
}
export function humanWorkflowName(name: string): string {
  for (const [prefix, title] of [
    ["factory-issue-", "Issue workflow"],
    ["factory-feedback-", "Feedback workflow"],
    ["factory-verify-", "Verification workflow"],
  ]) {
    if (name.startsWith(prefix)) return title;
  }
  return name;
}
