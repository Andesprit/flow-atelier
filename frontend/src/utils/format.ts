import type { LogEntry } from "@/types/task";

const MIN = 60_000;
const HR = 60 * MIN;
const DAY = 24 * HR;

// Upstream "treat bad dates as now" behavior can hand these helpers NaN or
// negative input; render a clean fallback instead of `NaN:NaN`/negative ms.
const FALLBACK = "-";
function isBadMs(ms: number): boolean {
  return !Number.isFinite(ms) || ms < 0;
}

export function fmtDuration(ms: number): string {
  if (isBadMs(ms)) return FALLBACK;
  if (ms < 1_000) return `${ms}ms`;
  if (ms < MIN) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < HR) {
    const m = Math.floor(ms / MIN);
    const s = Math.floor((ms % MIN) / 1000);
    return s ? `${m}m${s.toString().padStart(2, "0")}s` : `${m}m`;
  }
  const h = Math.floor(ms / HR);
  const m = Math.floor((ms % HR) / MIN);
  return m ? `${h}h${m.toString().padStart(2, "0")}m` : `${h}h`;
}

export function fmtRelative(ms: number, now = Date.now()): string {
  if (!Number.isFinite(ms)) return FALLBACK;
  const delta = now - ms;
  if (delta < MIN) return "just now";
  if (delta < HR) return `${Math.floor(delta / MIN)}m ago`;
  if (delta < DAY) return `${Math.floor(delta / HR)}h ago`;
  return `${Math.floor(delta / DAY)}d ago`;
}

/** Format milliseconds as m:ss or h:mm:ss */
export function fmtMSS(ms: number): string {
  if (isBadMs(ms)) return FALLBACK;
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function fmtClock(ms: number): string {
  if (isBadMs(ms)) return FALLBACK;
  const d = new Date(ms);
  return (
    d.getHours().toString().padStart(2, "0") +
    ":" +
    d.getMinutes().toString().padStart(2, "0") +
    ":" +
    d.getSeconds().toString().padStart(2, "0")
  );
}

/**
 * Render log lines as clipboard text: one `HH:MM:SS [task] text` line per
 * entry. Task-tagged lines are included even though the drawer's flat log box
 * hides them: the point is to get the whole run out in one go.
 */
export function logsToText(lines: LogEntry[]): string {
  return lines
    .map((l) => `${fmtClock(l.t)} ${l.task ? `[${l.task}] ` : ""}${l.text}`)
    .join("\n");
}
