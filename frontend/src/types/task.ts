import type { ToolType } from "./conduit";

export type { ToolType };

export type ColumnId = "todo" | "in_progress" | "done";

export type FlowTaskStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface HitlInput {
  name: string;
  description: string;
}

export interface HitlRequest {
  fromTool: ToolType;
  comment: string;
  taskName?: string;
  inputs?: HitlInput[];
  /** Identity of one asked question; set for live questions only. */
  key?: string;
}

export interface LogEntry {
  t: number;
  text: string;
  level: "info" | "ok" | "err" | "acc";
  task?: string;
}

export interface TaskFlow {
  flowId?: string;
  startedAt: number;
  currentTaskIndex: number;
  taskStatuses: Record<string, FlowTaskStatus>;
  logLines: LogEntry[];
  hitlRequest?: HitlRequest;
  hitlAnswers?: Record<string, string>;
}

export interface Task {
  name: string;
  description?: string;
  projectId: string;
  inputs: Record<string, string>;
  prompt?: string;
  tool?: ToolType;
  runPath?: string;
  createdAt: number;
  column: ColumnId;
  flow?: TaskFlow;
}
