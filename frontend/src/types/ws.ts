import type { LogEntry } from "./task";

// ── Client → Server ─────────────────────────────────────────────────────────

export interface WsRunMessage {
  type: "run";
  conduitName: string;
  inputs: Record<string, string>;
  runPath: string;
}

export interface WsHitlAnswerMessage {
  type: "hitl_answer";
  flowId: string;
  answers: Record<string, string>;
}

export interface WsCancelMessage {
  type: "cancel";
  flowId: string;
}

export interface WsResumeMessage {
  type: "resume";
  flowId: string;
}

/** Next turn for an interactive harness task, correlated by requestId. */
export interface WsAgentInputAnswerMessage {
  type: "agent_input_answer";
  flowId: string;
  requestId: string;
  answer: string;
}

export type ClientWsMessage =
  | WsRunMessage
  | WsHitlAnswerMessage
  | WsCancelMessage
  | WsResumeMessage
  | WsAgentInputAnswerMessage;

// ── Server → Client ─────────────────────────────────────────────────────────

export interface WsStartedMessage {
  type: "started";
  flowId: string;
  parentFlowId?: string;
  parentTask?: string;
  conduitName?: string;
}

export interface BackendTask {
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

export interface BackendLogEntry {
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

export interface WsLogMessage {
  type: "log";
  flowId: string;
  entry: BackendLogEntry | LogEntry;
}

export interface WsTaskStatusMessage {
  type: "step_status";
  flowId: string;
  step: string;
  status: string;
}

export interface WsTaskMessage {
  type: "step";
  flowId: string;
  task: string;
  step: BackendTask;
}

export interface WsHitlRequestInput {
  name: string;
  description: string;
}

export interface WsHitlRequestMessage {
  type: "hitl_request";
  flowId: string;
  task?: string;
  inputs?: WsHitlRequestInput[];
}

/** One chunk of an interactive agent's prose, streamed as it speaks. */
export interface WsAgentMessageMessage {
  type: "agent_message";
  flowId: string;
  task?: string;
  text: string;
}

/** The interactive agent handed the turn back and wants a reply. */
export interface WsAgentInputRequestMessage {
  type: "agent_input_request";
  flowId: string;
  task?: string;
  requestId: string;
  prompt: string;
}

export interface WsFlowCompleteMessage {
  type: "flow_complete";
  flowId: string;
}

export interface WsFlowFailedMessage {
  type: "flow_failed";
  flowId: string;
  error: string;
}

export interface WsErrorMessage {
  type: "error";
  flowId?: string;
  message: string;
}

export type ServerWsMessage =
  | WsStartedMessage
  | WsLogMessage
  | WsTaskStatusMessage
  | WsTaskMessage
  | WsHitlRequestMessage
  | WsAgentMessageMessage
  | WsAgentInputRequestMessage
  | WsFlowCompleteMessage
  | WsFlowFailedMessage
  | WsErrorMessage;
