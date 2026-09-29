import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { ToolType } from "@/types/conduit";
import { toolColor } from "@/constants/tools";
import { Repeat } from "lucide-react";
import { cn } from "@lib/cn";

export interface TaskNodeData extends Record<string, unknown> {
  idx: number;
  name: string;
  tool: ToolType;
  task: string;
  description: string;
  repeat?: number;
  until?: string | null;
  while?: string | null;
  onExhaust?: "complete" | "fail";
  conditional?: "match" | "not_match";
}

export function TaskNode({ data, selected }: NodeProps) {
  const d = data as TaskNodeData;
  const loopKind = d.until != null ? "until" : d.while != null ? "while" : null;
  const predicate = d.until ?? d.while;
  const match = predicate?.match(/^output\.(?:not_)?match\((.*)\)$/);
  const label = loopKind ? `↻${d.repeat} ${loopKind} ${match?.[1] ?? predicate}` : `↻${d.repeat}`;
  const warns = loopKind != null && d.onExhaust !== "fail";
  return (
    <div
      data-testid="task-node"
      data-node-name={d.name}
      className={cn(
        "relative w-[220px] min-h-[88px] border border-border bg-card font-mono text-foreground isolate",
        selected && "border-primary shadow-[0_0_0_1px_var(--color-primary)]",
      )}
    >
      {d.repeat != null && d.repeat > 1 && (
        <div
          className={cn("absolute -top-4 left-1/2 -translate-x-1/2 flex max-w-[220px] items-center gap-1 rounded-full border bg-card px-2 py-0.5", warns ? "border-warning/60 text-warning" : "border-ok/60 text-ok")}
          title={warns ? "Condition may remain unmet; dependent tasks will run on the last result. Set on_exhaust: fail to stop the run." : predicate ?? undefined}
          aria-label={warns ? `${label}. Dependents will run on the last result if the condition is never met.` : label}
        >
          <Repeat className="size-3 shrink-0" aria-hidden />
          <span className="truncate font-mono text-micro">{label}</span>
          {warns && <span aria-hidden="true">!</span>}
        </div>
      )}
      <div className="px-3 py-2">
        <span
          className="font-mono text-micro uppercase tracking-[0.14em]"
          style={{ color: toolColor(d.tool) }}
          data-testid="n-tool"
        >
          {d.tool}
        </span>
      </div>
      <div className="min-h-[20px] px-3 pb-1 pt-0.5 text-data">
        {d.name || " "}
      </div>
      <div className="min-h-[16px] px-3 pb-3 font-sans text-label leading-snug text-muted-foreground">
        {d.description || " "}
      </div>

      {/* Handle appearance is owned by styles/globals.css: react-flow's own CSS
          forces !important there, and restating the same properties here meant
          two !important rules racing on source order. */}
      <Handle type="target" position={Position.Left} className="!-translate-x-1/2" />
      <Handle type="source" position={Position.Right} className="!translate-x-1/2" />
    </div>
  );
}
