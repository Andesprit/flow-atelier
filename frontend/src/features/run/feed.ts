import { useEffect, useRef, useState } from "react";
import { openRunFeed, type RunFeed } from "@/services/api/runs";
import type { RunView, TaskLog, TaskLogRound, TaskLogUpdate } from "@/types/run";

/** Apply an update to the log it was computed against: new state, new lines. */
export function applyTaskLogUpdate(log: TaskLog, update: TaskLogUpdate): TaskLog {
  const rounds = new Map(log.rounds.map((r) => [r.iteration, r]));
  for (const patch of update.rounds) {
    const { append, ...state } = patch;
    const was: TaskLogRound | undefined = rounds.get(patch.iteration);
    rounds.set(patch.iteration, { ...state, lines: [...(was?.lines ?? []), ...append] });
  }
  return {
    ...log,
    status: update.status,
    reason: update.reason,
    of: update.of,
    rounds: [...rounds.values()].sort((a, b) => a.iteration - b.iteration),
  };
}

/**
 * The run's map and one task's log, kept current over one socket. `choose`
 * picks the task from the latest map, so a page that names no task can
 * follow whatever is running now.
 */
export function useRunFeed(
  flowId: string,
  choose: (view: RunView | undefined) => string | undefined,
) {
  const [view, setView] = useState<RunView>();
  const [log, setLog] = useState<TaskLog>();
  const [error, setError] = useState<string>();
  const feed = useRef<RunFeed>(undefined);

  useEffect(() => {
    const opened = openRunFeed(flowId, {
      onFlow: (next) => {
        setView(next);
        setError(undefined);
      },
      onTaskLog: (next) => {
        setLog(next);
        setError(undefined);
      },
      // An update only extends the log of the task it names; one for a task
      // the page has since left is dropped.
      onTaskUpdate: (update) =>
        setLog((prev) => (prev?.task === update.task ? applyTaskLogUpdate(prev, update) : prev)),
      onError: setError,
    });
    feed.current = opened;
    return () => opened.close();
  }, [flowId]);

  const task = choose(view);
  useEffect(() => {
    if (!task) return;
    setLog(undefined);
    feed.current?.watch(task);
  }, [flowId, task]);

  return { view, task, log: log?.task === task ? log : undefined, error };
}
