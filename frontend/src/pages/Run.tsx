import { Link, useParams, useSearchParams } from "react-router-dom";
import { fmtMSS } from "@/utils/format";
import { RunMap } from "@/features/run/RunMap";
import { TaskLog } from "@/features/run/TaskLog";
import { useRunFeed } from "@/features/run/feed";
import type { RunView } from "@/types/run";

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

/** The conduit name inside a flow id (`<date>_<uuid8>_<conduit>`). */
function conduitOf(flowId: string): string {
  return flowId.split("_").slice(2).join("_") || flowId;
}

export default function RunPage() {
  const { flowId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const asked = params.get("task") ?? undefined;
  const { view, task, log, error } = useRunFeed(flowId, (v) => asked ?? (v && defaultTask(v)));

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

  const running = view.status === "running";
  const selected = view.tasks.find((t) => t.name === task);
  const started = view.startedAt ? Date.parse(view.startedAt) : NaN;
  const ended = view.finishedAt ? Date.parse(view.finishedAt) : Date.now();
  const elapsed = ended >= started ? ` · ${fmtMSS(ended - started)}` : "";

  return (
    <div className="mx-auto flex max-w-[1100px] flex-col gap-6 px-6 py-8" data-testid="run-page">
      {view.parentFlowId && (
        <Link
          to={
            `/runs/${encodeURIComponent(view.parentFlowId)}` +
            (view.parentTask ? `?task=${encodeURIComponent(view.parentTask)}` : "")
          }
          title={view.parentFlowId}
          className="-mb-4 self-start font-mono text-data text-primary underline-offset-2 hover:underline"
        >
          ← {conduitOf(view.parentFlowId)}
          {view.parentTask ? ` · ${view.parentTask}` : ""}
        </Link>
      )}
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

      {selected && <TaskLog task={selected} log={log} error={error} live={running} />}
    </div>
  );
}
