import { useEffect } from "react";
import { waitingFlowIds, type LiveRun } from "@/hooks/useConduit";

/** The <title> in index.html; restored when the screen owning the runs unmounts. */
export const BASE_TITLE = "flow-atelier";

const MARK: Record<LiveRun["status"], string> = {
  running: "●",
  done: "✓",
  failed: "✗",
  cancelled: "■",
};

/**
 * Tab title for the current run state. This is what a developer sees from the
 * editor or terminal the run is working on, so state comes first and the
 * conduit name second. Child flows of a nested conduit are skipped: the parent
 * run already covers them, and its outcome is the one that matters.
 */
export function runTitle(runs: LiveRun[]): string {
  const top = runs.filter((r) => !r.parentFlowId);
  // A run parked on a question outranks one that is merely busy: nothing moves
  // until someone answers, and that someone is the person reading this title.
  const waiting = waitingFlowIds(runs);
  const parked = top.filter((r) => waiting.has(r.flowId));
  if (parked.length > 1) return `? ${parked.length} waiting · ${BASE_TITLE}`;
  if (parked.length === 1) return `? waiting · ${parked[0].conduitName}`;
  const running = top.filter((r) => r.status === "running");
  if (running.length > 1) return `● ${running.length} running · ${BASE_TITLE}`;
  if (running.length === 1) return `● running · ${running[0].conduitName}`;
  // Runs are kept in start order, so the last one is the most recently started.
  const last = top[top.length - 1];
  if (!last) return BASE_TITLE;
  return `${MARK[last.status]} ${last.status} · ${last.conduitName}`;
}

export function useRunTitle(runs: LiveRun[]): void {
  const title = runTitle(runs);
  useEffect(() => {
    document.title = title;
  }, [title]);
  useEffect(
    () => () => {
      document.title = BASE_TITLE;
    },
    [],
  );
}
