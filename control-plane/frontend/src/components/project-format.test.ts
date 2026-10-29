import { describe, expect, it } from "vitest";
import {
  humanWorkflowName,
  localTimestamp,
  relativeTimestamp,
} from "./project-format";

describe("project timestamps", () => {
  it("formats API epoch seconds and ISO timestamps in the user's local timezone", () => {
    const iso = "2026-10-29T17:42:06Z";
    const expected = new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short",
    }).format(new Date(iso));
    expect(localTimestamp(iso)).toBe(expected);
    expect(localTimestamp(Date.parse(iso) / 1000)).toBe(expected);
    expect(localTimestamp(0)).not.toBe("Unknown");
  });
  it("does not invent dates for absent or invalid timestamps", () => {
    for (const value of [null, undefined, "", "invalid", NaN, Infinity]) {
      expect(localTimestamp(value)).toBe("Unknown");
      expect(relativeTimestamp(value)).toBe("Unknown");
    }
  });
  it("shows relative past and future times using a supplied millisecond clock", () => {
    const now = Date.parse("2026-10-29T17:42:06Z");
    const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    expect(relativeTimestamp((now - 120000) / 1000, now)).toBe(
      format.format(-2, "minute"),
    );
    expect(relativeTimestamp("2026-10-30T17:42:06Z", now)).toBe(
      format.format(1, "day"),
    );
    expect(relativeTimestamp(0, 0)).toBe(format.format(0, "second"));
  });
});

it("labels known workflow families and preserves unknown workflow names", () => {
  expect(humanWorkflowName("factory-issue-backend-hash")).toBe(
    "Issue workflow",
  );
  expect(humanWorkflowName("factory-feedback-repo")).toBe("Feedback workflow");
  expect(humanWorkflowName("factory-verify-repo")).toBe(
    "Verification workflow",
  );
  expect(humanWorkflowName("hello-world")).toBe("hello-world");
});
