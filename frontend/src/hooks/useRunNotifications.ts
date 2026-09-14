import { useEffect, useRef } from "react";
import { waitingFlowIds, type LiveRun } from "@/hooks/useConduit";
import { fmtDuration } from "@/utils/format";
import { NOTIFY_STORAGE_KEY } from "@/constants/dashboard";

/** Notifications are on: the person opted in from the top bar and the browser allows them. */
export function notificationsOn(): boolean {
  return (
    typeof Notification !== "undefined" &&
    Notification.permission === "granted" &&
    localStorage.getItem(NOTIFY_STORAGE_KEY) === "1"
  );
}

type Phase = LiveRun["status"] | "waiting";

function phaseOf(run: LiveRun, waiting: Set<string>): Phase {
  return run.status === "running" && waiting.has(run.flowId) ? "waiting" : run.status;
}

// No entry for running (nothing to say yet) or cancelled (the person did that).
const HEADLINE: Partial<Record<Phase, string>> = {
  waiting: "? waiting",
  done: "✓ done",
  failed: "✗ failed",
};

/**
 * The question a run is parked on. It can sit on a nested child run: an
 * interactive task of a sub-conduit prompts under the child's own flow id.
 */
function question(root: LiveRun, runs: LiveRun[]): string {
  const byId = new Map(runs.map((r) => [r.flowId, r]));
  for (const r of runs) {
    let top = r;
    while (top.parentFlowId && byId.has(top.parentFlowId)) top = byId.get(top.parentFlowId)!;
    if (top.flowId !== root.flowId) continue;
    const text = r.agentRequests[0]?.prompt || r.hitlRequest?.comment;
    if (text) return text;
  }
  return "";
}

function bodyOf(run: LiveRun, phase: Phase, runs: LiveRun[]): string {
  const last = run.logLines[run.logLines.length - 1];
  if (phase === "waiting") return question(run, runs);
  // The engine's error is the last line since the failure path streams it.
  if (phase === "failed") return last?.text ?? "";
  return `in ${fmtDuration((last?.t ?? Date.now()) - run.startedAt)}`;
}

/** The person is looking at this page: the list and the drawer already show the change. */
function pageInView(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

/**
 * Desktop notification when a top-level run finishes, fails or parks on a
 * question while the page is not in view. The tab title (useRunTitle) covers
 * the glance at the tab strip; this covers the browser being behind the
 * editor. Off unless the person turned it on from the top bar, so the
 * permission prompt is always their own click.
 */
export function useRunNotifications(runs: LiveRun[]): void {
  const seen = useRef(new Map<string, Phase>());

  useEffect(() => {
    const waiting = waitingFlowIds(runs);
    for (const run of runs) {
      if (run.parentFlowId) continue;
      const phase = phaseOf(run, waiting);
      // Every run is born running, so one first seen in another state made
      // that transition inside a single render and still deserves its notice.
      const before = seen.current.get(run.flowId) ?? "running";
      seen.current.set(run.flowId, phase);
      const headline = HEADLINE[phase];
      if (phase === before || !headline) continue;
      if (!notificationsOn() || pageInView()) continue;
      try {
        const notice = new Notification(`${headline} · ${run.conduitName}`, {
          body: bodyOf(run, phase, runs),
          // A later state of the same run replaces its earlier notice, so an
          // answered question never leaves a stale "waiting" in the tray.
          tag: run.flowId,
        });
        notice.onclick = () => {
          window.focus();
          notice.close();
        };
      } catch {
        // Chrome on Android refuses page-created notifications outright.
      }
    }
  }, [runs]);
}
