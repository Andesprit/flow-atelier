import { fetchJson, USE_MOCK, BASE_URL } from "@/services/client";
import { mockGetFlows, mockGetFlowLogs } from "@/services/mock/flows";
import type { PriorFlow } from "@/types/flow";
import type { LogEntry } from "@/types/task";

interface BackendPriorFlow {
  flowId: string;
  conduitName: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: string;
}

interface BackendLogEntry {
  task: string;
  tool: string;
  iteration: number;
  of: number;
  command: string;
  stdout: string;
  stderr: string;
  exitCode: number;
  output: string;
  startedAt: string;
  finishedAt: string;
  durationSeconds: number;
  extra: Record<string, unknown>;
  tasks: BackendTask[];
}

interface BackendTask {
  kind: "thinking" | "tool_call" | "tool_result" | "interaction";
  timestamp: string;
  text: string;
  toolCallId: string;
  toolName: string;
  toolKind: string;
  toolStatus: string;
  toolInput: string;
  toolOutput: string;
  locations: string[];
}

// Parse an ISO timestamp to epoch ms, or null when missing/invalid. Returning
// null (instead of falling back to Date.now()) keeps a bad timestamp from
// masquerading as a fresh one and scrambling sort order.
export function parseTs(s: string | null | undefined): number | null {
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

function toPriorFlow(raw: BackendPriorFlow): PriorFlow {
  const startedAt = parseTs(raw.startedAt) ?? 0;
  const finishedAt = parseTs(raw.finishedAt) ?? undefined;
  return {
    flowId: raw.flowId,
    conduitName: raw.conduitName,
    startedAt,
    finishedAt,
    duration: finishedAt && startedAt ? finishedAt - startedAt : undefined,
    status: raw.status === "failed"
      ? "failed"
      : raw.status === "cancelled"
        ? "cancelled"
        : "done",
  };
}

function toLogEntries(raw: BackendLogEntry[]): LogEntry[] {
  if (raw.length === 0) return [];

  const lines: LogEntry[] = [];
  const firstT = parseTs(raw[0].startedAt) ?? 0;
  lines.push({ t: firstT, text: "▸ flow started", level: "info" });

  const seenTasks = new Set<string>();
  // Carry forward the last good timestamp so a bad one doesn't jump to "now".
  let lastT = firstT;

  for (const entry of raw) {
    const t = parseTs(entry.startedAt) ?? lastT;
    lastT = t;
    const level: LogEntry["level"] = entry.exitCode === 0 ? "ok" : "err";
    const task = entry.task || undefined;

    if (!seenTasks.has(entry.task)) {
      seenTasks.add(entry.task);
      lines.push({
        t,
        text: entry.exitCode === 0 ? `✓ ${entry.task}` : `✗ ${entry.task}`,
        level: entry.exitCode === 0 ? "ok" : "err",
        task,
      });
    }

    if (entry.command) {
      lines.push({ t, text: `$ ${entry.command}`, level: "info", task });
    }

    for (const rawLine of entry.stdout.split("\n")) {
      const text = rawLine.trim();
      if (text) lines.push({ t, text, level, task });
    }

    for (const rawLine of entry.stderr.split("\n")) {
      const text = rawLine.trim();
      if (text) lines.push({ t, text, level: "err", task });
    }

    if (entry.output && entry.output !== entry.stdout) {
      lines.push({ t, text: entry.output, level, task });
    }
  }

  const last = raw[raw.length - 1];
  const finalT = parseTs(last.finishedAt) ?? lastT;
  const allOk = raw.every((e) => e.exitCode === 0);
  lines.push({
    t: finalT,
    text: allOk ? "✓ flow complete" : "✗ flow failed",
    level: allOk ? "ok" : "err",
  });

  return lines;
}

function extractTasks(raw: BackendLogEntry[]) {
  const seen = new Set<string>();
  return raw
    .filter((entry) => {
      if (seen.has(entry.task)) return false;
      seen.add(entry.task);
      return true;
    })
    .map((entry) => ({
      name: entry.task,
      status: entry.exitCode === 0 ? ("done" as const) : ("failed" as const),
      durationMs: entry.durationSeconds ? Math.round(entry.durationSeconds * 1000) : undefined,
    }));
}

export async function getFlows(): Promise<PriorFlow[]> {
  if (import.meta.env.DEV) console.log(`[${USE_MOCK ? "mock" : "api"}] GET /flows`);
  if (USE_MOCK) {
    return mockGetFlows();
  }

  const raw = await fetchJson<BackendPriorFlow[]>(`${BASE_URL}/flows`, undefined, {
    method: "GET",
  });
  return raw.map(toPriorFlow);
}

interface FlowLogsResponse {
  runPath: string;
  logs: BackendLogEntry[];
  children?: string[];
}

export async function getFlowLogs(flowId: string) {
  if (import.meta.env.DEV) console.log(`[${USE_MOCK ? "mock" : "api"}] GET /flows/${flowId}/logs`);
  if (USE_MOCK) {
    return { logs: mockGetFlowLogs(flowId), tasks: [], runPath: undefined, children: [] };
  }

  const res = await fetchJson<FlowLogsResponse>(`${BASE_URL}/flows/${flowId}/logs`, undefined, {
    method: "GET",
  });
  return {
    logs: toLogEntries(res.logs),
    tasks: extractTasks(res.logs),
    runPath: res.runPath || undefined,
    children: res.children,
  };
}
