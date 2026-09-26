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
