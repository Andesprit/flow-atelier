import { useEffect, useState } from "react";
import { fetchRunSummary } from "@/services/api/runs";
import type { RunSummary, RunView } from "@/types/run";

/**
 * What the summary must agree with: the run's status, finish time and each
 * task's status and round. A log line changes none of these, so following a
 * log never refetches; a resume, a failure or a finish always does.
 */
export function summaryRevision(view: RunView): string {
  return JSON.stringify([
    view.status,
    view.finishedAt,
    view.tasks.map((t) => [t.name, t.status, t.iteration]),
  ]);
}

interface Read {
  flowId: string;
  revision: string;
  summary?: RunSummary;
  error?: string;
}

/**
 * The run's saved results and next step, read for `revision` of `flowId`.
 * A summary is only returned for the flow and revision it was read for, so a
 * route change or a newer run state never shows the previous advice; a late
 * answer for either is dropped.
 */
export function useRunSummary(flowId: string, revision: string | undefined) {
  const [read, setRead] = useState<Read>();
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (revision === undefined) return;
    let current = true;
    fetchRunSummary(flowId).then(
      (summary) => current && setRead({ flowId, revision, summary }),
      (e: unknown) =>
        current &&
        setRead({ flowId, revision, error: e instanceof Error ? e.message : String(e) }),
    );
    return () => {
      current = false;
    };
  }, [flowId, revision, attempt]);

  const fresh = read && read.flowId === flowId && read.revision === revision ? read : undefined;
  return {
    summary: fresh?.summary,
    error: fresh?.error,
    loading: revision !== undefined && !fresh,
    retry: () => {
      setRead(undefined);
      setAttempt((n) => n + 1);
    },
  };
}
