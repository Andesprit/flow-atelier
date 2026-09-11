import { useReducer, useRef, useCallback, useEffect } from "react";
import { RunConduitSocket } from "@/services/api/run-conduit";
import type {
  ServerWsMessage,
  BackendLogEntry,
  BackendTask,
  WsHitlRequestInput,
} from "@/types/ws";
import type { HitlInput, HitlRequest, LogEntry, FlowTaskStatus } from "@/types/task";

// ── Types ──────────────────────────────────────────────────────────────────

export type LiveRunStatus = "running" | "done" | "cancelled" | "failed";

/**
 * An interactive harness task waiting on the next turn. Distinct from
 * `hitlRequest`: this is an agent mid-ACP-session asking a question, answered
 * by correlation id, not a `tool:hitl` gate collecting named inputs.
 */
export interface AgentInputRequest {
  /**
   * The flow that asked. Carried on the request rather than inferred from the
   * run it is rendered under: a nested conduit's interactive task prompts
   * under the *child* flow id, and the answer has to go back to that same id
   * or the broker won't find the future to resolve.
   */
  flowId: string;
  requestId: string;
  prompt: string;
  taskName?: string;
}

export interface LiveRun {
  flowId: string;
  conduitName: string;
  startedAt: number;
  status: LiveRunStatus;
  logLines: LogEntry[];
  taskStatuses: Record<string, FlowTaskStatus>;
  hitlRequest?: HitlRequest;
  hitlAnswers?: Record<string, string>;
  /**
   * Every interactive turn still waiting on a reply. With max_concurrency > 1
   * two tasks in one flow can be parked at once, and each is answered by its
   * own request id — so this is a list, not a single slot.
   */
  agentRequests: AgentInputRequest[];
  runPath: string;
  inputs: Record<string, string>;
  parentFlowId?: string;
  parentTask?: string;
}

/**
 * Top-level flow ids parked on a person: the run itself, or a nested child run
 * of it, has a HITL gate or an interactive agent turn pending. Each one costs
 * wall-clock time until someone answers, so it is the state the recent flows
 * list, the drawer badge and the tab title lead with. Requests left on a run
 * that already stopped are ignored: nothing is waiting for that answer.
 */
export function waitingFlowIds(runs: LiveRun[]): Set<string> {
  const byId = new Map(runs.map((r) => [r.flowId, r]));
  const waiting = new Set<string>();
  for (const r of runs) {
    if (r.status !== "running" || (!r.hitlRequest && r.agentRequests.length === 0)) continue;
    let root = r;
    while (root.parentFlowId && byId.has(root.parentFlowId)) root = byId.get(root.parentFlowId)!;
    if (root.status === "running") waiting.add(root.flowId);
  }
  return waiting;
}

interface UseConduitOptions {
  onFlowStarted?: (flowId: string, conduitName: string) => void;
  onFlowComplete?: (flowId: string) => void;
  onError?: (message: string) => void;
}

// ── Translation helpers ───────────────────────────────────────────────────

export function isMockEntry(entry: BackendLogEntry | LogEntry): entry is LogEntry {
  return "text" in entry && "level" in entry;
}

export function backendLogToLines(entry: BackendLogEntry): LogEntry[] {
  const level: LogEntry["level"] = entry.exitCode === 0 ? "ok" : "err";
  const t = Date.parse(entry.startedAt) || Date.now();
  const task = entry.task || undefined;
  const lines: LogEntry[] = [];

  if (entry.command) {
    lines.push({ t, text: `$ ${entry.command}`, level: "info", task });
  }

  for (const raw of entry.stdout.split("\n")) {
    const text = raw.trim();
    if (text) lines.push({ t, text, level, task });
  }

  for (const raw of entry.stderr.split("\n")) {
    const text = raw.trim();
    if (text) lines.push({ t, text, level: "err", task });
  }

  if (entry.output && entry.output !== entry.stdout) {
    lines.push({ t, text: entry.output, level, task });
  }

  return lines;
}

export function taskToLine(detail: BackendTask, task?: string): LogEntry | null {
  const t = Date.parse(detail.timestamp) || Date.now();
  switch (detail.kind) {
    case "interaction":
      return detail.text ? { t, text: detail.text, level: "info", task } : null;
    case "thinking":
      return detail.text ? { t, text: detail.text, level: "info", task } : null;
    case "tool_call":
      return { t, text: `→ ${detail.toolName}`, level: "acc", task };
    case "tool_result": {
      const text = detail.toolOutput || detail.toolStatus;
      const level = detail.toolStatus === "error" ? "err" : "ok";
      return text ? { t, text, level, task } : null;
    }
    default:
      return null;
  }
}

export function mapStepStatus(raw: string): FlowTaskStatus {
  if (raw === "completed") return "done";
  if (raw === "cancelled") return "failed";
  return raw as FlowTaskStatus;
}

// ── Reducer ───────────────────────────────────────────────────────────────

export type Action =
  | { type: "WS_STARTED"; flowId: string; conduitName: string; runPath: string; inputs: Record<string, string>; parentFlowId?: string; parentTask?: string }
  | { type: "WS_STEP"; flowId: string; line: LogEntry }
  | { type: "WS_LOG"; flowId: string; lines: LogEntry[] }
  | { type: "WS_STEP_STATUS"; flowId: string; step: string; status: FlowTaskStatus; marker?: LogEntry }
  | { type: "WS_HITL_REQUEST"; flowId: string; inputs: WsHitlRequestInput[] | undefined; taskName?: string }
  | { type: "WS_AGENT_MESSAGE"; flowId: string; text: string; taskName?: string }
  | { type: "WS_AGENT_INPUT_REQUEST"; flowId: string; requestId: string; prompt: string; taskName?: string }
  | { type: "WS_FLOW_COMPLETE"; flowId: string }
  | { type: "WS_FLOW_FAILED"; flowId: string; error: string }
  | { type: "CANCEL"; flowId: string }
  | { type: "RESUME"; flowId: string; conduitName?: string }
  | { type: "ANSWER_HITL"; flowId: string; answers: Record<string, string> }
  | { type: "ANSWER_AGENT_INPUT"; flowId: string; requestId: string; answer: string };

export interface State {
  runs: Map<string, LiveRun>;
}

function updateRun(next: Map<string, LiveRun>, flowId: string, patch: Partial<LiveRun>) {
  const run = next.get(flowId);
  if (run) next.set(flowId, { ...run, ...patch });
}

export function reducer(state: State, action: Action): State {
  const next = new Map(state.runs);

  switch (action.type) {
    case "WS_STARTED": {
      const existing = next.get(action.flowId);
      if (existing) {
        // Resume case — run already exists, just add the log line
        next.set(action.flowId, {
          ...existing,
          logLines: [...existing.logLines, { t: Date.now(), text: "▸ flow resumed", level: "info" }],
        });
      } else if (action.parentFlowId) {
        // Child flow started by a sub-conduit task
        next.set(action.flowId, {
          flowId: action.flowId,
          conduitName: action.conduitName,
          startedAt: Date.now(),
          status: "running",
          logLines: [{ t: Date.now(), text: "▸ flow started", level: "info" }],
          taskStatuses: {},
          agentRequests: [],
          runPath: "",
          inputs: {},
          parentFlowId: action.parentFlowId,
          parentTask: action.parentTask,
        });
        // Add a marker to the parent's logs
        const parent = next.get(action.parentFlowId);
        if (parent) {
          next.set(action.parentFlowId, {
            ...parent,
            logLines: [...parent.logLines, {
              t: Date.now(),
              text: `▸ sub-conduit started: ${action.conduitName}`,
              level: "info" as const,
              task: action.parentTask,
            }],
          });
        }
      } else {
        // Fresh top-level run — create from pending metadata
        next.set(action.flowId, {
          flowId: action.flowId,
          conduitName: action.conduitName,
          startedAt: Date.now(),
          status: "running",
          logLines: [{ t: Date.now(), text: "▸ flow started", level: "info" }],
          taskStatuses: {},
          agentRequests: [],
          runPath: action.runPath,
          inputs: action.inputs,
        });
      }
      return { runs: next };
    }

    case "WS_STEP": {
      const run = next.get(action.flowId);
      if (run) next.set(action.flowId, { ...run, logLines: [...run.logLines, action.line] });
      return { runs: next };
    }

    case "WS_LOG": {
      const run = next.get(action.flowId);
      if (run) next.set(action.flowId, { ...run, logLines: [...run.logLines, ...action.lines] });
      return { runs: next };
    }

    case "WS_STEP_STATUS": {
      const run = next.get(action.flowId);
      if (run) {
        const logs = action.marker ? [...run.logLines, action.marker] : run.logLines;
        next.set(action.flowId, {
          ...run,
          logLines: logs,
          taskStatuses: { ...run.taskStatuses, [action.step]: action.status },
        });
      }
      return { runs: next };
    }

    case "WS_HITL_REQUEST": {
      const run = next.get(action.flowId);
      if (run) {
        const lastLog = run.logLines[run.logLines.length - 1];
        const hitlInputs: HitlInput[] = (action.inputs ?? []).map((i) => ({
          name: i.name,
          description: i.description,
        }));
        next.set(action.flowId, {
          ...run,
          hitlRequest: {
            fromTool: "tool:hitl" as const,
            comment: lastLog?.text ?? "",
            taskName: action.taskName,
            inputs: hitlInputs.length ? hitlInputs : undefined,
          },
        });
      }
      return { runs: next };
    }

    case "WS_AGENT_MESSAGE": {
      const run = next.get(action.flowId);
      if (run) {
        next.set(action.flowId, {
          ...run,
          logLines: [
            ...run.logLines,
            { t: Date.now(), text: action.text, level: "info", task: action.taskName },
          ],
        });
      }
      return { runs: next };
    }

    case "WS_AGENT_INPUT_REQUEST": {
      // Deliberately does not touch hitlRequest: an interactive agent turn and
      // a tool:hitl gate are separate states and can be pending at once.
      const run = next.get(action.flowId);
      if (run) {
        // Append rather than replace — a second interactive task prompting
        // does not mean the first one stopped waiting. Same id overwrites, so
        // a re-sent request can't show up twice.
        const others = run.agentRequests.filter((r) => r.requestId !== action.requestId);
        next.set(action.flowId, {
          ...run,
          agentRequests: [
            ...others,
            {
              flowId: action.flowId,
              requestId: action.requestId,
              prompt: action.prompt,
              taskName: action.taskName,
            },
          ],
        });
      }
      return { runs: next };
    }

    case "WS_FLOW_COMPLETE": {
      updateRun(next, action.flowId, {
        status: "done",
        agentRequests: [],
        logLines: [...(next.get(action.flowId)?.logLines ?? []), { t: Date.now(), text: "✓ flow complete", level: "ok" }],
      });
      return { runs: next };
    }

    case "WS_FLOW_FAILED": {
      const run = next.get(action.flowId);
      if (run) {
        // Don't overwrite a user-initiated cancel
        const status = run.status === "cancelled" ? "cancelled" : "failed";
        next.set(action.flowId, {
          ...run,
          status,
          agentRequests: [],
          logLines: [...run.logLines, { t: Date.now(), text: `✗ ${action.error}`, level: "err" }],
        });
      }
      return { runs: next };
    }

    case "CANCEL": {
      const run = next.get(action.flowId);
      if (run && run.status === "running") {
        // The flow is going away, so nothing will consume these answers.
        next.set(action.flowId, { ...run, status: "cancelled", agentRequests: [] });
      }
      return { runs: next };
    }

    case "RESUME": {
      const run = next.get(action.flowId);
      if (run) {
        // Preserve prior logs/statuses — WS_STARTED will append a "flow resumed" marker
        next.set(action.flowId, {
          ...run,
          status: "running",
          hitlRequest: undefined,
          hitlAnswers: undefined,
        });
      } else {
        // Prior flow from API — create a LiveRun with conduit name from action
        next.set(action.flowId, {
          flowId: action.flowId,
          conduitName: action.conduitName ?? "",
          startedAt: Date.now(),
          status: "running",
          logLines: [],
          taskStatuses: {},
          agentRequests: [],
          runPath: "",
          inputs: {},
        });
      }
      return { runs: next };
    }

    case "ANSWER_HITL": {
      const run = next.get(action.flowId);
      if (run) {
        next.set(action.flowId, { ...run, hitlAnswers: action.answers, hitlRequest: undefined });
      }
      return { runs: next };
    }

    case "ANSWER_AGENT_INPUT": {
      const run = next.get(action.flowId);
      // Only drop the request actually being answered — the other pending
      // turns belong to different tasks and stay answerable.
      const answered = run?.agentRequests.find((r) => r.requestId === action.requestId);
      if (run && answered) {
        next.set(action.flowId, {
          ...run,
          agentRequests: run.agentRequests.filter((r) => r.requestId !== action.requestId),
          logLines: [
            ...run.logLines,
            {
              t: Date.now(),
              text: `› ${action.answer}`,
              level: "acc",
              task: answered.taskName,
            },
          ],
        });
      }
      return { runs: next };
    }

    default:
      return state;
  }
}

// ── Hook ──────────────────────────────────────────────────────────────────

// Track what we've sent so we can route the `started` response back to the
// right conduitName/runPath/inputs.
// ponytail: FIFO correlation — assumes the server replies in send order. Robust
// for a single active run. Proper per-run correlation is blocked on the backend:
// RunMessage uses extra="forbid" (schemas/ws.py) so a client-generated run_id is
// rejected, and the `started` reply (routes/ws.py) echoes no client id. Upgrade
// path: add an optional run_id to RunMessage + StartedMessage on the server, then
// key `pendingRef` by that id instead of array order.
interface PendingRun {
  conduitName: string;
  runPath: string;
  inputs: Record<string, string>;
}

export function useConduit(opts: UseConduitOptions = {}) {
  const [state, dispatch] = useReducer(reducer, { runs: new Map() });
  const socketRef = useRef<RunConduitSocket | null>(null);
  const dispatchRef = useRef(dispatch);
  dispatchRef.current = dispatch;

  const onFlowStartedRef = useRef(opts.onFlowStarted);
  onFlowStartedRef.current = opts.onFlowStarted;

  const onFlowCompleteRef = useRef(opts.onFlowComplete);
  onFlowCompleteRef.current = opts.onFlowComplete;

  const onErrorRef = useRef(opts.onError);
  onErrorRef.current = opts.onError;

  const pendingRef = useRef<PendingRun[]>([]);

  // Close the live-run socket when the consumer unmounts so navigating between
  // screens doesn't leave idle connections receiving messages in the background.
  useEffect(() => {
    return () => {
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, []);

  const liveRuns = Array.from(state.runs.values());

  const handleWsMessage = useCallback((msg: ServerWsMessage) => {
    const d = dispatchRef.current;

    switch (msg.type) {
      case "started": {
        if (msg.parentFlowId) {
          // Child flow started by a sub-conduit — no pending run to match
          d({
            type: "WS_STARTED",
            flowId: msg.flowId,
            conduitName: msg.conduitName ?? "",
            runPath: "",
            inputs: {},
            parentFlowId: msg.parentFlowId,
            parentTask: msg.parentTask,
          });
        } else {
          // Top-level run — match to the pending run we sent earliest
          const pending = pendingRef.current.shift();
          const name = pending?.conduitName ?? "";
          d({
            type: "WS_STARTED",
            flowId: msg.flowId,
            conduitName: name,
            runPath: pending?.runPath ?? "",
            inputs: pending?.inputs ?? {},
          });
          onFlowStartedRef.current?.(msg.flowId, name);
        }
        break;
      }
      case "step": {
        const line = taskToLine(msg.step, msg.task);
        if (line) d({ type: "WS_STEP", flowId: msg.flowId, line });
        break;
      }
      case "log": {
        const lines = isMockEntry(msg.entry)
          ? [msg.entry]
          : backendLogToLines(msg.entry);
        if (lines.length) d({ type: "WS_LOG", flowId: msg.flowId, lines });
        break;
      }
      case "step_status": {
        const status = mapStepStatus(msg.status);
        let marker: LogEntry | undefined;
        if (status === "running") {
          marker = { t: Date.now(), text: `▸ ${msg.step}`, level: "info", task: msg.step };
        } else if (status === "done") {
          marker = { t: Date.now(), text: `✓ ${msg.step}`, level: "ok", task: msg.step };
        } else if (status === "failed") {
          marker = { t: Date.now(), text: `✗ ${msg.step}`, level: "err", task: msg.step };
        }
        d({ type: "WS_STEP_STATUS", flowId: msg.flowId, step: msg.step, status, marker });
        break;
      }
      case "hitl_request":
        d({ type: "WS_HITL_REQUEST", flowId: msg.flowId, inputs: msg.inputs, taskName: msg.task });
        break;
      case "agent_message":
        if (msg.text.trim()) {
          d({ type: "WS_AGENT_MESSAGE", flowId: msg.flowId, text: msg.text, taskName: msg.task });
        }
        break;
      case "agent_input_request":
        d({
          type: "WS_AGENT_INPUT_REQUEST",
          flowId: msg.flowId,
          requestId: msg.requestId,
          prompt: msg.prompt,
          taskName: msg.task,
        });
        break;
      case "flow_complete":
        d({ type: "WS_FLOW_COMPLETE", flowId: msg.flowId });
        onFlowCompleteRef.current?.(msg.flowId);
        break;
      case "flow_failed":
        d({ type: "WS_FLOW_FAILED", flowId: msg.flowId, error: msg.error });
        onFlowCompleteRef.current?.(msg.flowId);
        break;
      case "error":
        console.error("[ws] server error:", msg.message);
        if (msg.flowId) {
          d({ type: "WS_LOG", flowId: msg.flowId, lines: [{ t: Date.now(), text: `✗ ${msg.message}`, level: "err" as const }] });
        }
        break;
    }
  }, []);

  const getOrCreateSocket = useCallback(() => {
    const existing = socketRef.current;
    if (existing && existing.readyState <= WebSocket.OPEN) return existing;

    const sock = new RunConduitSocket();
    sock.onMessage = handleWsMessage;
    sock.onClose = () => {
      if (socketRef.current === sock) socketRef.current = null;
    };
    socketRef.current = sock;
    return sock;
  }, [handleWsMessage]);

  const run = useCallback(
    (conduitName: string, inputs: Record<string, string>, runPath: string) => {
      // Stash metadata so we can build the LiveRun when `started` arrives
      const pending: PendingRun = { conduitName, runPath, inputs };
      pendingRef.current.push(pending);

      const sock = getOrCreateSocket();
      sock
        .waitForOpen()
        .then(() => {
          sock.send({ type: "run", conduitName, inputs, runPath });
        })
        .catch((err) => {
          const i = pendingRef.current.indexOf(pending);
          if (i >= 0) pendingRef.current.splice(i, 1);
          onErrorRef.current?.(
            err instanceof Error ? err.message : "failed to start run",
          );
        });
    },
    [getOrCreateSocket],
  );

  const cancel = useCallback(
    (flowId: string) => {
      dispatch({ type: "CANCEL", flowId });
      const sock = socketRef.current;
      if (!sock) return;
      try {
        sock.send({ type: "cancel", flowId });
      } catch (err) {
        onErrorRef.current?.(
          err instanceof Error ? err.message : "failed to cancel run",
        );
      }
    },
    [],
  );

  const resume = useCallback(
    (flowId: string, conduitName?: string) => {
      dispatch({ type: "RESUME", flowId, conduitName });
      const sock = getOrCreateSocket();
      sock
        .waitForOpen()
        .then(() => {
          sock.send({ type: "resume", flowId });
        })
        .catch((err) => {
          onErrorRef.current?.(
            err instanceof Error ? err.message : "failed to resume run",
          );
        });
    },
    [getOrCreateSocket],
  );

  const answerHITL = useCallback(
    (flowId: string, answers: Record<string, string>) => {
      dispatch({ type: "ANSWER_HITL", flowId, answers });
      const sock = socketRef.current;
      if (!sock) return;
      try {
        sock.send({ type: "hitl_answer", flowId, answers });
      } catch (err) {
        onErrorRef.current?.(
          err instanceof Error ? err.message : "failed to send answer",
        );
      }
    },
    [],
  );

  const answerAgentInput = useCallback(
    (flowId: string, requestId: string, answer: string) => {
      // Send first: an agent parked on this turn only unblocks if the answer
      // reaches it, so a failed send has to leave the prompt on screen.
      try {
        const sock = socketRef.current;
        if (!sock) throw new Error("not connected — cannot send answer");
        sock.send({ type: "agent_input_answer", flowId, requestId, answer });
      } catch (err) {
        onErrorRef.current?.(
          err instanceof Error ? err.message : "failed to send answer",
        );
        return;
      }
      dispatch({ type: "ANSWER_AGENT_INPUT", flowId, requestId, answer });
    },
    [],
  );

  return { run, cancel, resume, answerHITL, answerAgentInput, liveRuns } as const;
}
