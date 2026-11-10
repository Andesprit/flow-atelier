/** One task on the run page's map: what it depends on and how far it got. */
export interface RunTask {
  name: string;
  tool: string;
  description: string;
  dependsOn: string[];
  /** pending | running | completed | failed | skipped | cancelled */
  status: string;
  iteration: number;
  of: number;
}

/** `GET /flows/:id` — every task of one run, with its progress. */
export interface RunView {
  flowId: string;
  conduitName: string;
  /** running | completed | failed | stopped */
  status: string;
  startedAt: string | null;
  finishedAt: string | null;
  runPath: string | null;
  /** For a sub-run, the run whose `parentTask` started it. */
  parentFlowId?: string | null;
  parentTask?: string | null;
  currentTasks: string[];
  tasks: RunTask[];
}

/** One line of a task's log. `text` arrives condensed and credential-masked. */
export interface TaskLogLine {
  at: string | null;
  /** run, read, edit, search, thought, said, out, err, done, failed, ... */
  kind: string;
  text: string;
  level: "info" | "warn" | "error";
}

export interface TaskLogRound {
  iteration: number;
  /** running | completed | failed, or the task's own status when it stopped mid-round */
  status: string;
  startedAt: string | null;
  durationSeconds: number | null;
  exitCode: number | null;
  /** For a `tool:conduit` task, the sub-run this round started. */
  childFlowId?: string | null;
  lines: TaskLogLine[];
}

/** `GET /flows/:id/tasks/:task/log` — one task's rounds, one line per action. */
export interface TaskLog {
  task: string;
  tool: string;
  status: string;
  reason: string | null;
  of: number;
  rounds: TaskLogRound[];
}

/** A round's current state and the lines it gained since the last update. */
export interface TaskRoundPatch {
  iteration: number;
  status: string;
  startedAt: string | null;
  durationSeconds: number | null;
  exitCode: number | null;
  childFlowId?: string | null;
  append: TaskLogLine[];
}

/** What changed in a task's log: its state, and each round that grew. */
export interface TaskLogUpdate {
  task: string;
  status: string;
  reason: string | null;
  of: number;
  rounds: TaskRoundPatch[];
}

/** One task's saved result. `text` arrives credential-masked and bounded. */
export interface RunResult {
  task: string;
  status: string;
  /** text: saved text · empty: saved "" · none: saved null · missing: no entry */
  state: "text" | "empty" | "none" | "missing";
  text: string | null;
  chars: number;
  truncated: boolean;
}

export interface RunFailure {
  task: string;
  kind: string | null;
  exitCode: number | null;
  reason: string | null;
  channel: string | null;
  excerpt: string | null;
  truncated: boolean;
}

/** A suggested command. The page only shows and copies it; nothing runs it. */
export interface RunNextStep {
  what: string;
  command: string | null;
  /** The command with its project folder (or store) selected, ready to paste. */
  copyText: string | null;
  sideEffects: string | null;
  /** Starts, resumes or stops work, rather than only reading. */
  acts: boolean;
}

/** `GET /flows/:id/summary` — saved results and what to do next. */
export interface RunSummary {
  flowId: string;
  status: string;
  observedState: string;
  observedCertainty: string;
  observedNote: string;
  settled: boolean;
  settledNote: string | null;
  final: boolean;
  workspace: string | null;
  store: string;
  storeNote: string | null;
  parentFlowId: string | null;
  parentTask: string | null;
  results: RunResult[];
  resultsUnavailable: string | null;
  fullResultsCommand: string;
  failures: RunFailure[];
  unavailable: string[];
  nextSteps: RunNextStep[];
}
