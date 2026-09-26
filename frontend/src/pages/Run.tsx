import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { fmtMSS } from "@/utils/format";
import { getRunView } from "@/services/api/runs";
import { RunMap } from "@/features/run/RunMap";
import { TaskLog } from "@/features/run/TaskLog";
import type { RunView } from "@/types/run";

const POLL_MS = 2000;

const STATUS_STYLE: Record<string, string> = {
  running: "border-primary text-primary",
  completed: "border-ok text-ok",
  failed: "border-destructive text-destructive",
};

/** The task to show when the link names none: what is running, else what failed. */
export function defaultTask(view: RunView): string | undefined {
  return (
    view.currentTasks.find((name) => view.tasks.some((t) => t.name === name)) ??
    view.tasks.find((t) => t.status === "failed")?.name ??
    view.tasks[0]?.name
  );
}

export default function RunPage() {
  const { flowId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [view, setView] = useState<RunView>();
  const [error, setError] = useState<string>();

  const running = view?.status === "running";

  useEffect(() => {
    let ignore = false;
    let inFlight = false;
    const load = () => {
      if (inFlight) return;
      inFlight = true;
      getRunView(flowId)
        .then((next) => {
          if (ignore) return;
          setView(next);
          setError(undefined);
        })
        .catch((e: unknown) => {
          if (!ignore) setError(e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          inFlight = false;
        });
    };
    load();
    if (view && !running) {
      return () => {
        ignore = true;
      };
    }
    const id = setInterval(load, POLL_MS);
    return () => {
      ignore = true;
      clearInterval(id);
    };
    // Keyed on whether a view has loaded, not on the view itself, so polling
    // restarts only when the run starts or stops and not on every update.
  }, [flowId, running, view === undefined]);

  if (error && !view) {
    return (
      <div className="mx-auto max-w-[1100px] px-6 py-10">
        <p className="font-mono text-data text-destructive">Couldn't load run {flowId}: {error}</p>
      </div>
    );
  }
  if (!view) {
    return (
      <div className="mx-auto max-w-[1100px] px-6 py-10 font-mono text-data text-muted-foreground">
        Loading run…
      </div>
    );
  }

  const selectedName = params.get("task") ?? defaultTask(view);
  const selected = view.tasks.find((t) => t.name === selectedName);
  const started = view.startedAt ? Date.parse(view.startedAt) : NaN;
  const ended = view.finishedAt ? Date.parse(view.finishedAt) : Date.now();
  const elapsed = ended >= started ? ` · ${fmtMSS(ended - started)}` : "";

  return (
    <div className="mx-auto flex max-w-[1100px] flex-col gap-6 px-6 py-8" data-testid="run-page">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="font-display text-head">{view.conduitName || view.flowId}</h1>
        <span
          className={`rounded-full border px-3 py-1 font-mono text-label tracking-[0.1em] uppercase ${
            STATUS_STYLE[view.status] ?? "border-border text-muted-foreground"
          }`}
          data-testid="run-status"
        >
          {view.status}
          {elapsed}
        </span>
        <span className="font-mono text-label text-muted-foreground select-all">{view.flowId}</span>
        {view.runPath && (
          <span className="font-mono text-label text-muted-foreground">{view.runPath}</span>
        )}
      </header>

      {view.tasks.length === 0 ? (
        <p className="font-mono text-data text-muted-foreground">This run has no tasks to show yet.</p>
      ) : (
        <RunMap
          tasks={view.tasks}
          current={running ? view.currentTasks : []}
          selected={selected?.name}
          onSelect={(task) => setParams({ task }, { replace: true })}
        />
      )}

      {selected && <TaskLog flowId={view.flowId} task={selected} live={running} />}
    </div>
  );
}
