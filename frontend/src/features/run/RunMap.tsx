import { useMemo } from "react";
import { cn } from "@/lib/cn";
import type { RunTask } from "@/types/run";
import { hereBox, layoutRunMap, NODE_H, NODE_W } from "./layout";

interface Props {
  tasks: RunTask[];
  /** Tasks running now; framed as "you are here" while the run is live. */
  current: string[];
  selected?: string;
  onSelect: (task: string) => void;
}

export function statusLabel(task: RunTask): string {
  switch (task.status) {
    case "running":
      return task.of > 1 ? `round ${task.iteration} of ${task.of}` : "running";
    case "completed":
      return task.iteration > 1 ? `done · ${task.iteration} rounds` : "done";
    case "failed":
      return task.of > 1 ? `failed · round ${task.iteration}` : "failed";
    case "pending":
      return "waiting";
    default:
      return task.status;
  }
}

const NODE_STYLE: Record<string, string> = {
  completed: "border-ok/60 bg-ok/10",
  running: "border-2 border-primary bg-card",
  failed: "border-destructive bg-destructive/10",
  pending: "border-dashed border-border bg-transparent text-muted-foreground",
  skipped: "border-dashed border-border bg-transparent text-muted-foreground",
  cancelled: "border-dashed border-border bg-transparent text-muted-foreground",
};

const STATUS_TEXT: Record<string, string> = {
  completed: "text-ok",
  running: "text-primary",
  failed: "text-destructive",
};

export function RunMap({ tasks, current, selected, onSelect }: Props) {
  const layout = useMemo(() => layoutRunMap(tasks), [tasks]);
  const box = hereBox(layout.nodes, current);
  const byName = new Map(tasks.map((t) => [t.name, t]));

  return (
    <div className="overflow-x-auto rounded-sm border border-border bg-card/40" data-testid="run-map">
      <div className="relative" style={{ width: layout.width, height: layout.height }}>
        <svg
          width={layout.width}
          height={layout.height}
          className="absolute inset-0"
          aria-hidden="true"
        >
          {layout.edges.map((e) => (
            <path
              key={`${e.from}->${e.to}`}
              d={e.d}
              fill="none"
              stroke="var(--color-edge)"
              strokeWidth={1.5}
              strokeDasharray={byName.get(e.to)?.status === "pending" ? "4 4" : undefined}
            />
          ))}
          {box && (
            <rect
              x={box.x}
              y={box.y}
              width={box.width}
              height={box.height}
              rx={8}
              fill="none"
              stroke="var(--color-primary)"
              strokeWidth={1.5}
              strokeDasharray="6 4"
            />
          )}
        </svg>
        {box && (
          <span
            className="absolute -translate-x-1/2 whitespace-nowrap rounded-full bg-primary px-2.5 py-0.5 font-mono text-mini font-semibold tracking-[0.12em] text-primary-foreground"
            style={{ left: box.x + box.width / 2, top: box.y - 10 }}
          >
            YOU ARE HERE
          </span>
        )}
        {layout.nodes.map((n) => {
          const task = byName.get(n.name)!;
          const isSelected = n.name === selected;
          return (
            <button
              key={n.name}
              type="button"
              aria-pressed={isSelected}
              title={task.description || undefined}
              onClick={() => onSelect(n.name)}
              className={cn(
                "absolute flex flex-col items-start justify-center gap-0.5 rounded-sm border px-3 text-left font-mono transition-colors hover:border-foreground/60",
                NODE_STYLE[task.status] ?? "border-border bg-card",
                isSelected && "ring-2 ring-foreground ring-offset-2 ring-offset-background",
              )}
              style={{ left: n.x, top: n.y, width: NODE_W, height: NODE_H }}
              data-testid={`run-map-node-${n.name}`}
            >
              <span className="w-full truncate text-data text-foreground">{n.name}</span>
              <span className={`text-label ${STATUS_TEXT[task.status] ?? "text-muted-foreground"}`}>
                {statusLabel(task)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
