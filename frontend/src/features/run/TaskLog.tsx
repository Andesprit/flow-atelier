import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/cn";
import { fmtClock } from "@/utils/format";
import { toolColor } from "@/constants/tools";
import type { ToolType } from "@/types/conduit";
import type { RunTask, TaskLog as TaskLogData, TaskLogLine, TaskLogRound } from "@/types/run";
import { statusLabel } from "./RunMap";

interface Props {
  task: RunTask;
  /** The task's log as the feed last pushed it; undefined until it arrives. */
  log?: TaskLogData;
  error?: string;
  /** The run is still going, so new lines keep arriving. */
  live: boolean;
}

// Shown in the narrow kind column. Output lines leave it blank so a command's
// output reads like the terminal it came from.
const KIND_LABEL: Record<string, string> = { out: "", err: "err" };

function fmtSeconds(s: number): string {
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(Math.round(s) / 60);
  return `${m}m ${(Math.round(s) % 60).toString().padStart(2, "0")}s`;
}

function roundSummary(round: TaskLogRound): string {
  const took = round.durationSeconds != null ? ` · ${fmtSeconds(round.durationSeconds)}` : "";
  if (round.status === "completed") return `done${took}`;
  if (round.status === "failed") return `failed${took}`;
  return round.status;
}

export function TaskLog({ task, log, error, live }: Props) {
  const [query, setQuery] = useState("");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [showThoughts, setShowThoughts] = useState(false);
  // Rounds whose open state the reader flipped from the default (last one open).
  const [flipped, setFlipped] = useState<Set<number>>(new Set());

  useEffect(() => {
    setFlipped(new Set());
    setShowThoughts(false);
  }, [task.name]);

  const q = query.trim().toLowerCase();
  const filtering = q !== "" || problemsOnly;
  const keep = (line: TaskLogLine) =>
    (showThoughts || line.kind !== "thought") &&
    (!problemsOnly || line.level !== "info") &&
    (!q || line.text.toLowerCase().includes(q));

  const rounds = log?.rounds ?? [];
  const multi = rounds.length > 1 || (log?.of ?? 1) > 1;
  const hiddenThoughts = showThoughts
    ? 0
    : rounds.reduce((n, r) => n + r.lines.filter((l) => l.kind === "thought").length, 0);
  const shown = rounds
    .map((round, i) => ({ round, isLast: i === rounds.length - 1, lines: round.lines.filter(keep) }))
    .filter((r) => !filtering || r.lines.length > 0);

  const toggle = (iteration: number) =>
    setFlipped((prev) => {
      const next = new Set(prev);
      if (next.has(iteration)) next.delete(iteration);
      else next.add(iteration);
      return next;
    });

  let body;
  if (error) {
    body = <p className="px-4 py-3 text-body text-destructive">Couldn't load this log: {error}</p>;
  } else if (!log) {
    body = <p className="px-4 py-3 text-body text-muted-foreground">Loading…</p>;
  } else if (log.status === "skipped") {
    body = (
      <p className="px-4 py-3 text-body text-muted-foreground">
        Skipped{log.reason ? `: ${log.reason}` : "."}
      </p>
    );
  } else if (rounds.length === 0) {
    body = <p className="px-4 py-3 text-body text-muted-foreground">Not started yet.</p>;
  } else if (filtering && shown.length === 0) {
    body = <p className="px-4 py-3 text-body text-muted-foreground">Nothing matches.</p>;
  } else {
    body = shown.map(({ round, isLast, lines }) => {
      const open = filtering || !multi || isLast !== flipped.has(round.iteration);
      const problems = round.lines.filter((l) => l.level !== "info").length;
      return (
        <div key={round.iteration} data-testid={`task-log-round-${round.iteration}`}>
          {multi && (
            <div className="flex items-center hover:bg-muted/40">
              <button
                type="button"
                aria-expanded={open}
                onClick={() => toggle(round.iteration)}
                className="flex flex-1 items-center gap-3 px-4 py-1.5 text-left font-mono text-data"
              >
                <span className="w-3 text-muted-foreground">{open ? "▾" : "▸"}</span>
                <span className={cn("w-20", isLast && "font-semibold")}>Round {round.iteration}</span>
                <RoundMarker status={round.status} />
                <span
                  className={cn(
                    round.status === "running" ? "text-primary" : "text-muted-foreground",
                  )}
                >
                  {roundSummary(round)}
                  {problems > 0 && round.status !== "failed"
                    ? ` · ${problems} ${problems === 1 ? "warning" : "warnings"}`
                    : ""}
                </span>
              </button>
              {round.childFlowId && <SubRunLink flowId={round.childFlowId} />}
            </div>
          )}
          {open && (
            <div className={cn(multi && "ml-9 border-l-2 border-border")}>
              {!multi && round.childFlowId && <SubRunLink flowId={round.childFlowId} />}
              {lines.map((line, i) => (
                <LogLine key={i} line={line} />
              ))}
              {round.status === "running" && lines.length === 0 && (
                <p className="px-4 py-1.5 text-body text-muted-foreground">
                  {task.tool === "tool:bash"
                    ? "Running. Nothing printed yet."
                    : task.tool === "tool:conduit"
                      ? "Running as a sub-run. Open it to follow each step."
                      : "Working. Actions show here as they happen."}
                </p>
              )}
            </div>
          )}
        </div>
      );
    });
  }

  return (
    <section
      aria-label={`${task.name} log`}
      className="rounded-sm border border-border bg-card"
      data-testid="task-log"
    >
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
        <span className="font-mono text-data font-semibold">{task.name}</span>
        {task.tool && (
          <span className="font-mono text-label" style={{ color: toolColor(task.tool as ToolType) }}>
            {task.tool}
          </span>
        )}
        <span className="text-body text-muted-foreground">{statusLabel(task)}</span>
        {live && task.status === "running" && (
          <span className="flex items-center gap-1.5 font-mono text-mini tracking-[0.12em] text-primary">
            <span className="h-2 w-2 rounded-full bg-primary motion-safe:animate-pulse" />
            LIVE
          </span>
        )}
        <span className="flex-1" />
        <input
          aria-label="Search this log"
          placeholder="Search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="h-8 w-52 rounded-sm border border-border-strong bg-background px-2.5 text-body text-foreground"
        />
        <label className="flex items-center gap-1.5 text-body text-muted-foreground">
          <input
            type="checkbox"
            checked={problemsOnly}
            onChange={(e) => setProblemsOnly(e.target.checked)}
          />
          Problems only
        </label>
      </div>
      <div className="py-2">{body}</div>
      {hiddenThoughts > 0 && (
        <div className="border-t border-border px-4 py-2 text-body text-muted-foreground">
          {hiddenThoughts} {hiddenThoughts === 1 ? "thought" : "thoughts"} hidden.{" "}
          <button
            type="button"
            onClick={() => setShowThoughts(true)}
            className="text-primary underline-offset-2 hover:underline"
          >
            Show them
          </button>
        </div>
      )}
    </section>
  );
}

function SubRunLink({ flowId }: { flowId: string }) {
  return (
    <Link
      to={`/runs/${encodeURIComponent(flowId)}`}
      title={flowId}
      className="shrink-0 px-4 py-1.5 font-mono text-data text-primary underline-offset-2 hover:underline"
    >
      Open sub-run →
    </Link>
  );
}

function RoundMarker({ status }: { status: string }) {
  const color =
    status === "completed"
      ? "bg-ok"
      : status === "failed"
        ? "bg-destructive"
        : status === "running"
          ? "bg-primary"
          : "bg-muted-foreground";
  return <span className={cn("h-2 w-2 shrink-0 rounded-[2px]", color)} aria-hidden="true" />;
}

function LogLine({ line }: { line: TaskLogLine }) {
  const at = line.at ? Date.parse(line.at) : NaN;
  const said = line.kind === "said";
  return (
    <div
      className={cn(
        "grid grid-cols-[8px_64px_56px_minmax(0,1fr)] items-baseline gap-x-3 px-4 py-1 font-mono",
        line.level === "error" && "bg-destructive/10",
        line.level === "warn" && "bg-warning/10",
      ) + " text-body"}
    >
      <span
        aria-hidden="true"
        className={cn(
          "h-2 w-2 self-center rounded-[2px]",
          line.level === "error" && "bg-destructive",
          line.level === "warn" && "bg-warning",
        )}
      />
      <span className="text-muted-foreground">{Number.isNaN(at) ? "" : fmtClock(at)}</span>
      <span className="text-muted-foreground">{KIND_LABEL[line.kind] ?? line.kind}</span>
      <span
        className={cn(
          "min-w-0 whitespace-pre-wrap break-words",
          said && "font-sans text-data",
          (line.kind === "done" || line.kind === "prompt") && "text-muted-foreground",
          line.kind === "thought" && "italic text-muted-foreground",
        )}
      >
        {line.text}
      </span>
    </div>
  );
}
