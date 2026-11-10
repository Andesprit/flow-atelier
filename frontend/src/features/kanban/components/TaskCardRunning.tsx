import { useEffect, useRef, useCallback } from "react";
import type { Task } from "@/types/task";
import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import { useTaskStore } from "@/runner";
import { toolColor } from "@/constants/tools";
import { cn } from "@/lib/cn";

interface Props {
  task: Task;
  selected?: boolean;
  onClick?: () => void;
  /** Stops the run; no button is shown without it. */
  onCancel?: () => void;
}

export function TaskCardRunning({ task: initialTask, selected, onClick, onCancel }: Props) {
  const selectThisTask = useCallback((s: { tasks: Task[] }) => s.tasks.find((t) => t.name === initialTask.name), [initialTask.name]);
  const liveTask = useTaskStore(selectThisTask);
  const task = liveTask ?? initialTask;

  const { conduits } = useConduits();
  const conduit = getConduitSync(task.name, conduits);
  const isConduit = !!conduit;
  const desc = task.description ?? conduit?.description;
  const flow = task.flow!;
  const order = conduit?.tasks ?? [];
  const doneCount = order.filter(
    (t) => flow.taskStatuses[t.name] === "done",
  ).length;
  const pct = order.length ? (doneCount / order.length) * 100 : 0;
  const hitl = flow.hitlRequest;

  const logsRef = useRef<HTMLDivElement>(null);
  const tail = flow.logLines.slice(-4);
  // Absolute index of the first visible line, so keys stay stable as the
  // window slides (avoids React reusing rows for different content).
  const tailBase = flow.logLines.length - tail.length;

  useEffect(() => {
    if (logsRef.current) {
      logsRef.current.scrollTop = logsRef.current.scrollHeight;
    }
  }, [tail.length]);

  return (
    <article
      data-testid="task-card-running"
      data-task-id={task.name}
      data-selected={selected || undefined}
      onClick={onClick}
      className={cn(
        "group min-w-0 overflow-hidden cursor-pointer border bg-card px-3 py-2.5",
        hitl ? "border-warning border-2 bg-warning/5" : "border-border",
        selected && "border-primary shadow-[0_0_0_1px_var(--color-primary)]",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 font-mono text-data leading-snug text-foreground truncate">
          {task.name}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {onCancel && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onCancel(); }}
              data-testid="cancel-button"
              aria-label="Cancel task"
              className="flex h-6 items-center px-1 font-mono text-micro uppercase tracking-[0.12em] text-muted-foreground hover:text-destructive focus-visible:outline-2 focus-visible:outline-primary"
            >
              cancel
            </button>
          )}
          {hitl ? (
            <span className="shrink-0 font-mono text-micro uppercase tracking-[0.12em] text-warning">
              waiting for review
            </span>
          ) : (
            <span
              className="shrink-0 font-mono text-micro uppercase tracking-[0.12em]"
              style={{ color: `var(--color-type-${isConduit ? "conduit" : "task"})` }}
            >
              {isConduit ? "conduit" : "task"}
            </span>
          )}
        </div>
      </div>
      {desc && (
        <div className="mt-0.5 text-body leading-snug text-muted-foreground truncate">
          {desc}
        </div>
      )}
      {!isConduit && task.tool && (
        <div className="mt-0.5 flex items-center gap-1.5">
          <span
            className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ backgroundColor: toolColor(task.tool) }}
          />
          <span className="font-mono text-micro text-muted-foreground truncate">{task.tool}</span>
        </div>
      )}

      <div className="mt-2 h-[2px] bg-border/60">
        <div
          className="h-full bg-primary transition-[width] duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-1 flex justify-between font-mono text-micro text-muted-foreground">
        <span>{doneCount}/{order.length}</span>
        <span className="flex items-center gap-1">
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/50 motion-reduce:hidden" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary" />
          </span>
          live
        </span>
      </div>

      <div
        ref={logsRef}
        className="relative mt-2 max-h-[68px] overflow-hidden border border-border/60 bg-background px-2 py-1.5 font-mono text-micro leading-[1.5] text-muted-foreground"
      >
        {tail.map((line, i) => (
          <div
            key={tailBase + i}
            className={cn(
              "truncate",
              line.level === "ok" && "text-[color:var(--color-ok)]",
              line.level === "acc" && "text-primary",
              line.level === "err" && "text-destructive",
            )}
          >
            {line.text}
          </div>
        ))}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-5 bg-gradient-to-t from-card to-transparent" />
      </div>
    </article>
  );
}
