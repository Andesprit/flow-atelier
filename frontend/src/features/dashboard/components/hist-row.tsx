import React from "react";
import { fmtDuration, fmtRelative } from "@/utils/format";
import { cn } from "@lib/cn";

export interface Row {
  flowId: string;
  conduit: string;
  startedAt: number;
  duration: number | undefined;
  state: "running" | "done" | "failed" | "cancelled";
  tag: string;
  isConduit?: boolean;
}

export const HistRow = React.forwardRef<
  HTMLLIElement,
  { row: Row; onClick?: () => void }
>(function HistRow({ row, onClick }, ref) {
  const running = row.state === "running";
  return (
    <li
      ref={ref}
      data-testid={running ? "hist-row-running" : "hist-row"}
      data-state={row.state}
      onClick={onClick}
      className={cn(
        "grid grid-cols-[14px_1fr_90px_80px_60px] items-center gap-4 py-3 pr-5 font-mono text-body",
        onClick && "cursor-pointer hover:bg-muted/40 transition-colors",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          row.state === "running" &&
            "bg-primary shadow-[0_0_0_3px_oklch(0.78_0.155_70/0.18)] motion-safe:animate-pulse",
          row.state === "done" && "bg-[color:var(--color-ok)]",
          row.state === "failed" && "bg-destructive",
          row.state === "cancelled" && "bg-muted-foreground",
        )}
      />
      <div className="min-w-0">
        <div className="truncate text-foreground">
          {row.conduit}
        </div>
        <div className="mt-0.5">
          <span
            className="font-mono text-micro uppercase tracking-[0.12em]"
            style={{ color: `var(--color-type-${row.isConduit ? "conduit" : "task"})` }}
          >
            {row.isConduit ? "conduit" : "task"}
          </span>
        </div>
      </div>
      <span className="text-muted-foreground">
        {fmtDuration(row.duration ?? 0)}
      </span>
      <span className="text-muted-foreground">
        {running ? "running" : fmtRelative(row.startedAt)}
      </span>
      <span
        className={cn(
          "text-right text-mini uppercase tracking-[0.14em]",
          running ? "text-primary" : "text-muted-foreground",
          row.state === "failed" && "text-destructive",
          row.tag === "waiting" && "text-warning",
        )}
      >
        {row.tag}
      </span>
    </li>
  );
});
