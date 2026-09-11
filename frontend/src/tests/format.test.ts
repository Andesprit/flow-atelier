import { describe, it, expect } from "vitest";
import { fmtDuration, fmtRelative, fmtClock, fmtMSS, logsToText } from "@/utils/format";

const MIN = 60_000;
const HR = 60 * MIN;
const DAY = 24 * HR;

describe("fmtDuration", () => {
  it("renders 0 as 0ms", () => {
    expect(fmtDuration(0)).toBe("0ms");
  });

  it("renders sub-second values as milliseconds", () => {
    expect(fmtDuration(500)).toBe("500ms");
  });

  it("renders 999ms at boundary", () => {
    expect(fmtDuration(999)).toBe("999ms");
  });

  it("renders 1 second as 1.0s", () => {
    expect(fmtDuration(1000)).toBe("1.0s");
  });

  it("renders 5000ms as 5.0s", () => {
    expect(fmtDuration(5000)).toBe("5.0s");
  });

  it("renders 59400ms as 59.4s (just under 1 minute)", () => {
    expect(fmtDuration(59_400)).toBe("59.4s");
  });

  it("renders 1 minute as 1m", () => {
    expect(fmtDuration(MIN)).toBe("1m");
  });

  it("renders 2 minutes as 2m", () => {
    expect(fmtDuration(2 * MIN)).toBe("2m");
  });

  it("renders 1m30s with seconds", () => {
    expect(fmtDuration(90_000)).toBe("1m30s");
  });

  it("renders exactly 1 hour as 1h", () => {
    expect(fmtDuration(HR)).toBe("1h");
  });

  it("renders 2 hours as 2h", () => {
    expect(fmtDuration(2 * HR)).toBe("2h");
  });

  it("renders 1h30m with minutes", () => {
    expect(fmtDuration(HR + 30 * MIN)).toBe("1h30m");
  });

  it("returns fallback for NaN and negative input", () => {
    expect(fmtDuration(NaN)).toBe("-");
    expect(fmtDuration(-1)).toBe("-");
    expect(fmtDuration(Infinity)).toBe("-");
  });
});

describe("fmtRelative", () => {
  const now = 1_000_000_000;

  it("returns 'just now' for timestamps under 1 minute ago", () => {
    expect(fmtRelative(now - 30_000, now)).toBe("just now");
  });

  it("returns 'just now' at exactly 0ms ago", () => {
    expect(fmtRelative(now, now)).toBe("just now");
  });

  it("returns minutes ago", () => {
    expect(fmtRelative(now - 5 * MIN, now)).toBe("5m ago");
  });

  it("returns 59m ago at minute boundary", () => {
    expect(fmtRelative(now - 59 * MIN, now)).toBe("59m ago");
  });

  it("returns hours ago", () => {
    expect(fmtRelative(now - 2 * HR, now)).toBe("2h ago");
  });

  it("returns 23h ago at hour boundary", () => {
    expect(fmtRelative(now - 23 * HR, now)).toBe("23h ago");
  });

  it("returns days ago", () => {
    expect(fmtRelative(now - 3 * DAY, now)).toBe("3d ago");
  });

  it("uses Date.now() when now parameter is omitted", () => {
    const result = fmtRelative(Date.now() - 500);
    expect(result).toBe("just now");
  });
});

describe("fmtClock", () => {
  it("zero-pads single-digit hours, minutes, and seconds", () => {
    const ms = new Date(2024, 0, 1, 1, 2, 3).getTime();
    expect(fmtClock(ms)).toBe("01:02:03");
  });

  it("formats midnight as 00:00:00", () => {
    const ms = new Date(2024, 0, 1, 0, 0, 0).getTime();
    expect(fmtClock(ms)).toBe("00:00:00");
  });

  it("formats noon as 12:00:00", () => {
    const ms = new Date(2024, 0, 1, 12, 0, 0).getTime();
    expect(fmtClock(ms)).toBe("12:00:00");
  });

  it("formats double-digit values correctly", () => {
    const ms = new Date(2024, 0, 1, 23, 59, 59).getTime();
    expect(fmtClock(ms)).toBe("23:59:59");
  });
});

describe("fmtMSS", () => {
  it("formats zero as 0:00", () => {
    expect(fmtMSS(0)).toBe("0:00");
  });

  it("formats seconds only", () => {
    expect(fmtMSS(45_000)).toBe("0:45");
  });

  it("zero-pads seconds", () => {
    expect(fmtMSS(5_000)).toBe("0:05");
  });

  it("formats exactly 1 minute", () => {
    expect(fmtMSS(60_000)).toBe("1:00");
  });

  it("formats minutes with padded seconds", () => {
    expect(fmtMSS(125_000)).toBe("2:05");
  });

  it("formats exactly 1 hour as h:mm:ss", () => {
    expect(fmtMSS(3_600_000)).toBe("1:00:00");
  });

  it("formats h:mm:ss", () => {
    expect(fmtMSS(3_665_000)).toBe("1:01:05");
  });

  it("formats multi-hour", () => {
    expect(fmtMSS(7_384_000)).toBe("2:03:04");
  });

  it("returns fallback for NaN and negative input", () => {
    expect(fmtMSS(NaN)).toBe("-");
    expect(fmtMSS(-1)).toBe("-");
  });
});

describe("formatter guards", () => {
  it("fmtClock returns fallback for bad input", () => {
    expect(fmtClock(NaN)).toBe("-");
    expect(fmtClock(-1)).toBe("-");
  });

  it("fmtRelative returns fallback for non-finite input", () => {
    expect(fmtRelative(NaN)).toBe("-");
  });
});

describe("logsToText", () => {
  it("renders one clock-stamped line per entry and tags task lines", () => {
    const t = new Date(2024, 0, 1, 1, 2, 3).getTime();
    expect(
      logsToText([
        { t, text: "▸ flow started", level: "info" },
        { t: t + 1000, text: "hello", level: "ok", task: "build" },
      ]),
    ).toBe("01:02:03 ▸ flow started\n01:02:04 [build] hello");
  });

  it("returns an empty string for no lines", () => {
    expect(logsToText([])).toBe("");
  });
});
