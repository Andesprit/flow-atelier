import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cn";
import { toast } from "sonner";
import { fmtClock, fmtDuration, fmtMSS, logsToText } from "@/utils/format";
import { ChevronRight } from "lucide-react";
import type { LogEntry, HitlRequest, FlowTaskStatus } from "@/types/task";
import type { Conduit } from "@/types/conduit";
import type { AgentInputRequest, LiveRun } from "@/hooks/useConduit";

export interface FlowDrawerTask {
  name: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  tool?: string;
  childFlowId?: string;
  durationMs?: number;
}

export interface FlowDrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  badge?: string;
  /** Shown with a copy button: it is what `atelier logs/status/stop <id>` take. */
  flowId?: string;
  tasks?: FlowDrawerTask[];
  logLines?: LogEntry[];
  startedAt?: number;
  duration?: number;
  hitl?: HitlRequest;
  hitlResponses?: string[];
  onRespondToHitl?: (answers: Record<string, string>) => void;
  /**
   * Interactive harness turns awaiting a reply — separate from `hitl`. May
   * include turns from nested child flows, so the handler is given the whole
   * request: only it knows which flow to send the answer back to.
   */
  agentInputs?: AgentInputRequest[];
  onAnswerAgentInput?: (request: AgentInputRequest, answer: string) => void;
  onCancel?: () => void;
  onResume?: () => void;
  onRemove?: () => void;
  /** Start a fresh run with the same conduit, inputs and working directory. */
  onRunAgain?: () => void;
  /** What the run was started with; shown so two runs of one conduit can be told apart. */
  inputs?: Record<string, string>;
  runPath?: string;
  onOpenPath?: () => void;
  hideCancel?: boolean;
  childRuns?: LiveRun[];
}

/** Extract per-task durations from log markers (▸ start → ✓/✗ end). */
export function computeTaskDurations(
  logLines: LogEntry[] | undefined,
  taskNames: string[],
): Map<string, number> {
  const starts = new Map<string, number>();
  const ends = new Map<string, number>();
  for (const line of logLines ?? []) {
    if (!line.task) continue;
    if (line.text.startsWith("▸ ")) starts.set(line.task, line.t);
    else if (line.text.startsWith("✓ ") || line.text.startsWith("✗ ")) ends.set(line.task, line.t);
  }
  const now = Date.now();
  const result = new Map<string, number>();
  for (const name of taskNames) {
    const start = starts.get(name);
    if (start == null) continue;
    const end = ends.get(name);
    result.set(name, (end ?? now) - start);
  }
  return result;
}

/**
 * Reconstruct nested child-run data from a parent's flat log entries.
 * Uses conduit definitions to identify which tasks belong to sub-conduits
 * and partitions the parent's flat logs accordingly.
 */
export function buildChildRunsFromLogs(
  flowLogs: LogEntry[],
  parentConduitDef: Conduit | undefined,
  allConduits: Conduit[],
  parentFlowId: string,
): LiveRun[] {
  if (!parentConduitDef) return [];

  const subConduitTasks = parentConduitDef.tasks.filter(
    (st) => st.tool === "tool:conduit",
  );

  return subConduitTasks.flatMap((st) => {
    const childConduit = allConduits.find((c) => c.name === st.task);
    if (!childConduit) return [];

    const childTaskNames = new Set(childConduit.tasks.map((t) => t.name));
    const childLogLines = flowLogs.filter(
      (l) => l.task && childTaskNames.has(l.task),
    );

    const taskStatuses: Record<string, FlowTaskStatus> = {};
    for (const ct of childConduit.tasks) {
      taskStatuses[ct.name] = statusFromMarkers(childLogLines, ct.name, true);
    }

    const allDone = Object.values(taskStatuses).every((s) => s === "done");

    return [
      {
        flowId: `prior-child:${st.name}`,
        conduitName: childConduit.name,
        startedAt: childLogLines[0]?.t ?? 0,
        status: (allDone ? "done" : "failed") as LiveRun["status"],
        logLines: childLogLines,
        taskStatuses,
        agentRequests: [],
        runPath: "",
        inputs: {},
        parentFlowId,
        parentTask: st.name,
      },
    ];
  });
}

/** Derive a task's status from its log markers. */
export function statusFromMarkers(logs: LogEntry[], taskName: string, flowDone = false): FlowTaskStatus {
  const markers = logs.filter((l) => l.task === taskName);
  if (markers.some((l) => l.text.startsWith("✓ "))) return "done";
  if (markers.some((l) => l.text.startsWith("✗ "))) return "failed";
  if (markers.some((l) => l.text.startsWith("▸ "))) return "running";
  return flowDone ? "skipped" : "pending";
}

export function FlowDrawer({
  open,
  onClose,
  title,
  subtitle,
  badge,
  flowId,
  tasks,
  logLines,
  startedAt,
  duration,
  hitl,
  hitlResponses,
  onRespondToHitl,
  agentInputs,
  onAnswerAgentInput,
  onCancel,
  onResume,
  onRemove,
  onRunAgain,
  inputs,
  runPath,
  onOpenPath,
  hideCancel,
  childRuns,
}: FlowDrawerProps) {
  const hasLogs = (logLines?.length ?? 0) > 0;

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        className="flex w-full flex-col sm:max-w-[520px]"
        data-testid="flow-drawer"
      >
        <SheetHeader>
          <SheetTitle className="font-mono">{title}</SheetTitle>
          {subtitle && <SheetDescription>{subtitle}</SheetDescription>}
          {(badge || hasLogs || onOpenPath) && (
            <div className="flex items-center gap-2 pt-2">
              {badge && (
                <Badge variant="outline" data-testid="flow-drawer-badge">
                  {badge}
                </Badge>
              )}
              <div className="ml-auto flex items-center gap-2">
                {hasLogs && (
                  <CopyButton
                    label="copy logs"
                    getText={() => logsToText(logLines ?? [])}
                    testId="flow-drawer-copy-logs"
                  />
                )}
                {onOpenPath && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="font-mono text-micro"
                    onClick={onOpenPath}
                    data-testid="flow-drawer-open-path"
                  >
                    open path
                  </Button>
                )}
              </div>
            </div>
          )}
          {flowId && (
            <div
              className="flex items-center gap-2 pt-2 font-mono text-mini text-muted-foreground"
              data-testid="flow-drawer-id"
            >
              <span className="min-w-0 truncate select-all" title={flowId}>
                {flowId}
              </span>
              <CopyButton
                label="copy id"
                getText={() => flowId}
                testId="flow-drawer-copy-id"
              />
            </div>
          )}
        </SheetHeader>

        <ScrollArea className="flex-1 px-6 py-4">
          <StartedWith runPath={runPath} inputs={inputs} />

          {tasks && tasks.length > 0 ? (
            <>
              <ExpandableTasks
                tasks={tasks}
                logLines={logLines}
                childRuns={childRuns}
              />

              {logLines && logLines.length > 0 && (
                <LogsSection
                  logLines={logLines}
                  startedAt={startedAt}
                  duration={duration}
                />
              )}

              {hitl && (
                <HitlSection
                  hitl={hitl}
                  responses={hitlResponses}
                  onRespond={onRespondToHitl}
                />
              )}
            </>
          ) : (
            logLines && logLines.length > 0 ? (
              <LogsSection
                logLines={logLines}
                startedAt={startedAt}
                duration={duration}
              />
            ) : (
              <div className="font-mono text-label text-muted-foreground">
                No flow data available.
              </div>
            )
          )}

          {agentInputs?.map((req) => (
            // Keyed by request id, so a new turn mounts a fresh, empty box and
            // an answered one takes its draft away with it.
            <AgentInputSection
              key={req.requestId}
              agentInput={req}
              onAnswer={
                onAnswerAgentInput
                  ? (answer) => onAnswerAgentInput(req, answer)
                  : undefined
              }
            />
          ))}
        </ScrollArea>

        <div className="flex items-center justify-between gap-3 border-t border-border p-4">
          <div className="font-mono text-mini text-muted-foreground">
          </div>
          {onRemove && (
            <Button
              variant="ghost"
              size="sm"
              data-testid="drawer-remove-button"
              onClick={() => {
                onRemove();
                onClose();
              }}
              className="text-muted-foreground hover:text-destructive"
            >
              remove
            </Button>
          )}
          {onCancel && !hideCancel && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                onCancel();
                onClose();
              }}
              className="border-destructive/50 text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              cancel
            </Button>
          )}
          {onRunAgain && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                onRunAgain();
                onClose();
              }}
              data-testid="drawer-run-again-button"
              className="font-mono text-micro"
            >
              run again
            </Button>
          )}
          {onResume && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                onResume();
                onClose();
              }}
              data-testid="drawer-resume-button"
              className="border-primary/50 text-primary hover:bg-primary/10 hover:text-primary"
            >
              resume
            </Button>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/* ── What the run was started with ───────────────────────────────────────── */

/**
 * The working directory and input values a run was given. Two runs of one
 * conduit are otherwise identical in the list and the drawer, and the path is
 * what a developer needs to `cd` to the run. Values are selectable.
 */
function StartedWith({
  runPath,
  inputs,
}: {
  runPath?: string;
  inputs?: Record<string, string>;
}) {
  const entries = Object.entries(inputs ?? {});
  if (!runPath && entries.length === 0) return null;

  return (
    <section className="mb-4" data-testid="flow-drawer-started-with">
      <div className="mb-2 font-mono text-mini uppercase tracking-[0.14em] text-muted-foreground">
        started with
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 font-mono text-label">
        {runPath && (
          <>
            <dt className="text-muted-foreground">working directory</dt>
            <dd className="break-all select-all text-foreground">{runPath}</dd>
          </>
        )}
        {entries.map(([name, value]) => (
          <Fragment key={name}>
            <dt className="text-muted-foreground">{name}</dt>
            <dd className="whitespace-pre-wrap break-all select-all text-foreground">{value}</dd>
          </Fragment>
        ))}
      </dl>
    </section>
  );
}

/* ── Copy to clipboard ────────────────────────────────────────────────────── */

/**
 * Copies `getText()` on click and flips its label to "copied" for a moment.
 * `navigator.clipboard` only exists in secure contexts (https or localhost),
 * so a page served over http on a LAN address gets a toast instead of a silent
 * no-op; the text next to the button stays selectable for a manual copy.
 */
function CopyButton({
  label,
  getText,
  testId,
}: {
  label: string;
  getText: () => string;
  testId: string;
}) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  const copy = () => {
    if (!navigator.clipboard) {
      toast.error("Clipboard unavailable here. Select the text and copy it by hand.");
      return;
    }
    navigator.clipboard
      .writeText(getText())
      .then(() => setCopied(true))
      .catch(() => toast.error("Couldn't copy to the clipboard"));
  };

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="font-mono text-micro"
      onClick={copy}
      data-testid={testId}
    >
      {copied ? "copied" : label}
    </Button>
  );
}

/* ── Expandable task list ─────────────────────────────────────────────────── */

function ExpandableTasks({
  tasks,
  logLines,
  childRuns,
}: {
  tasks: FlowDrawerTask[];
  logLines?: LogEntry[];
  childRuns?: LiveRun[];
}) {
  const logsByTask = useMemo(() => {
    const map = new Map<string, LogEntry[]>();
    for (const line of logLines ?? []) {
      if (line.task) {
        const arr = map.get(line.task) ?? [];
        arr.push(line);
        map.set(line.task, arr);
      }
    }
    return map;
  }, [logLines]);

  const [open, setOpen] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {};
    for (const s of tasks) {
      if (s.status === "running") initial[s.name] = true;
    }
    return initial;
  });

  useEffect(() => {
    for (const s of tasks) {
      if (s.status === "running" && !open[s.name]) {
        setOpen((prev) => ({ ...prev, [s.name]: true }));
      }
    }
  }, [tasks, open]);

  const toggle = (name: string) =>
    setOpen((prev) => ({ ...prev, [name]: !prev[name] }));

  // Build a map of child runs by parentTask for quick lookup
  const childByTask = useMemo(() => {
    const map = new Map<string, LiveRun>();
    for (const cr of childRuns ?? []) {
      if (cr.parentTask) map.set(cr.parentTask, cr);
    }
    return map;
  }, [childRuns]);

  return (
    <section className="mb-4">
      <div className="mb-2 font-mono text-mini uppercase tracking-[0.14em] text-muted-foreground">
        tasks
      </div>
      <ul className="space-y-0.5">
        {tasks.map((st) => {
          const taskLogs = logsByTask.get(st.name);
          const isOpen = !!open[st.name];
          const childRun = st.childFlowId
            ? childByTask.get(st.name)
            : undefined;
          return (
            <li key={st.name}>
              <button
                type="button"
                onClick={() => toggle(st.name)}
                className="flex w-full items-center gap-2 rounded px-1 py-1.5 font-mono text-label text-left hover:bg-muted/40 transition-colors"
              >
                <ChevronRight
                  className={cn(
                    "h-3 w-3 shrink-0 transition-transform",
                    isOpen && "rotate-90",
                  )}
                />
                <span
                  className={cn(
                    "inline-block h-1.5 w-1.5 rounded-full shrink-0",
                    st.status === "running" && "bg-primary",
                    st.status === "done" && "bg-[color:var(--color-ok)]",
                    st.status === "failed" && "bg-destructive",
                    st.status === "skipped" && "bg-muted-foreground/60",
                    st.status === "pending" && "bg-border",
                  )}
                />
                <span className="flex-1 truncate">{st.name}</span>
                {st.durationMs != null && (
                  <span className="shrink-0 text-mini text-muted-foreground">
                    {fmtMSS(st.durationMs)}
                  </span>
                )}
                <span className="text-muted-foreground">{st.status}</span>
              </button>
              {isOpen && childRun && (
                <NestedConduitTasks childRun={childRun} />
              )}
              {isOpen && !childRun && taskLogs && taskLogs.length > 0 && (
                <div className="ml-5 border-l border-border pl-3 pb-1">
                  <LogLines
                    lines={taskLogs}
                    testId="task-logs"
                    className="max-h-[200px] overflow-auto bg-background px-2 py-1.5 font-mono text-label leading-relaxed text-muted-foreground"
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/* ── Nested sub-conduit task list ───────────────────────────────────────────── */

function NestedConduitTasks({ childRun }: { childRun: LiveRun }) {
  const childTaskNames = Object.keys(childRun.taskStatuses);
  const childLogsByTask = useMemo(() => {
    const map = new Map<string, LogEntry[]>();
    for (const line of childRun.logLines) {
      if (line.task) {
        const arr = map.get(line.task) ?? [];
        arr.push(line);
        map.set(line.task, arr);
      }
    }
    return map;
  }, [childRun.logLines]);

  const childDurations = useMemo(
    () => computeTaskDurations(childRun.logLines, childTaskNames),
    [childRun.logLines, childTaskNames],
  );

  const [open, setOpen] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {};
    for (const name of childTaskNames) {
      if (childRun.taskStatuses[name] === "running") initial[name] = true;
    }
    return initial;
  });

  const toggle = (name: string) =>
    setOpen((prev) => ({ ...prev, [name]: !prev[name] }));

  return (
    <div className="ml-5 border-l border-primary/30 pl-3 pb-1">
      <div className="mb-1 font-mono text-micro uppercase tracking-[0.12em] text-primary/70">
        {childRun.conduitName}
      </div>
      <ul className="space-y-0.5">
        {childTaskNames.map((name) => {
          const status = childRun.taskStatuses[name];
          const taskLogs = childLogsByTask.get(name);
          const isOpen = !!open[name];
          return (
            <li key={name}>
              <button
                type="button"
                onClick={() => toggle(name)}
                className="flex w-full items-center gap-2 rounded px-1 py-1 font-mono text-mini text-left hover:bg-muted/40 transition-colors"
              >
                <ChevronRight
                  className={cn(
                    "h-2.5 w-2.5 shrink-0 transition-transform",
                    isOpen && "rotate-90",
                  )}
                />
                <span
                  className={cn(
                    "inline-block h-1.5 w-1.5 rounded-full shrink-0",
                    status === "running" && "bg-primary",
                    status === "done" && "bg-[color:var(--color-ok)]",
                    status === "failed" && "bg-destructive",
                    status === "skipped" && "bg-muted-foreground/60",
                    status === "pending" && "bg-border",
                  )}
                />
                <span className="flex-1 truncate">{name}</span>
                {childDurations.has(name) && (
                  <span className="shrink-0 text-micro text-muted-foreground">
                    {fmtMSS(childDurations.get(name)!)}
                  </span>
                )}
                <span className="text-muted-foreground">{status}</span>
              </button>
              {isOpen && taskLogs && taskLogs.length > 0 && (
                <div className="ml-4 border-l border-border pl-2 pb-1">
                  <LogLines
                    lines={taskLogs}
                    testId="child-task-logs"
                    className="max-h-[160px] overflow-auto bg-background px-2 py-1 font-mono text-mini leading-relaxed text-muted-foreground"
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ── Log box ──────────────────────────────────────────────────────────────── */

// How close to the bottom (px) still counts as "at the tail". Absorbs the
// sub-pixel scrollTop browsers report under zoom.
const TAIL_SLACK_PX = 4;

/**
 * One scrollable box of log lines that follows its own tail, like a terminal:
 * a new line scrolls it to the bottom, unless the reader has scrolled up, in
 * which case it stays put until they scroll back down. Each box owns its pin,
 * so two tasks streaming at once (max_concurrency > 1) follow independently.
 */
function LogLines({
  lines,
  className,
  testId,
}: {
  lines: LogEntry[];
  className: string;
  testId: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  return (
    <div
      ref={ref}
      data-testid={testId}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight <= TAIL_SLACK_PX;
      }}
      className={className}
    >
      {lines.map((line, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap break-all",
            line.level === "ok" && "text-[color:var(--color-ok)]",
            line.level === "acc" && "text-primary",
            line.level === "err" && "text-destructive",
          )}
        >
          <span className="text-muted-foreground">
            {fmtClock(line.t)}{" "}
          </span>
          {line.text}
        </div>
      ))}
    </div>
  );
}

/* ── Flat logs box showing global / marker lines only ──────────────────────── */

const MARKER_RE = /^[▸✓✗]/;

function LogsSection({
  logLines,
  startedAt,
  duration,
}: {
  logLines: LogEntry[];
  startedAt?: number;
  duration?: number;
}) {
  // Show only: untagged lines (flow started/complete) and task start/end markers
  const globalLines = useMemo(
    () => logLines.filter((l) => !l.task || MARKER_RE.test(l.text)),
    [logLines],
  );

  if (globalLines.length === 0) return null;

  return (
    <section className="mb-4">
      <div className="mb-2 font-mono text-mini uppercase tracking-[0.14em] text-muted-foreground">
        logs
      </div>
      <LogLines
        lines={globalLines}
        testId="drawer-logs"
        className="max-h-[260px] overflow-auto border border-border bg-background px-3 py-2 font-mono text-label leading-relaxed text-muted-foreground"
      />
      {startedAt && (
        <div className="mt-1 font-mono text-mini text-muted-foreground">
          {globalLines.length} lines · {fmtDuration(duration ?? Date.now() - startedAt)} elapsed
        </div>
      )}
    </section>
  );
}

/* ── Interactive agent turn ───────────────────────────────────────────────── */

/**
 * Reply box for one interactive harness task. The agent is parked mid-session
 * waiting on this one answer, so it's a single free-form field — no named
 * inputs, and none of the HITL form's state.
 *
 * The caller keys this by request id, which is what resets the draft between
 * turns. The text is deliberately left alone on submit: if the send fails the
 * prompt stays up, and retyping the answer would be the user's punishment.
 */
export function AgentInputSection({
  agentInput,
  onAnswer,
}: {
  agentInput: AgentInputRequest;
  onAnswer?: (answer: string) => void;
}) {
  const [value, setValue] = useState("");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const answer = value.trim();
    if (!answer) return;
    onAnswer?.(answer);
  };

  return (
    <section className="mb-4" data-testid="agent-input">
      <div className="mb-2 font-mono text-mini uppercase tracking-[0.14em] text-primary">
        {agentInput.taskName
          ? `agent is waiting on you · ${agentInput.taskName}`
          : "agent is waiting on you"}
      </div>
      <div className="font-mono text-label text-muted-foreground">
        {agentInput.prompt}
      </div>
      <form onSubmit={submit} className="mt-2.5 space-y-2">
        <textarea
          data-testid="agent-input-field"
          aria-label="reply to the agent"
          rows={3}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Type your reply…"
          className="w-full resize-y border border-border bg-transparent p-2 font-mono text-label text-foreground focus:border-primary"
        />
        <Button
          type="submit"
          size="sm"
          className="w-full text-micro"
          data-testid="agent-input-submit"
        >
          send reply
        </Button>
      </form>
    </section>
  );
}

/* ── HITL form ────────────────────────────────────────────────────────────── */

function HitlSection({
  hitl,
  responses,
  onRespond,
}: {
  hitl: HitlRequest;
  responses?: string[];
  onRespond?: (answers: Record<string, string>) => void;
}) {
  const fields: { name: string; description: string }[] =
    hitl.inputs && hitl.inputs.length > 0
      ? hitl.inputs
      : [{ name: "response", description: hitl.comment || "Your response" }];

  const [values, setValues] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<string[]>([]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed: Record<string, string> = {};
    const empty: string[] = [];
    for (const f of fields) {
      const v = (values[f.name] ?? "").trim();
      if (!v) { empty.push(f.name); continue; }
      trimmed[f.name] = v;
    }
    if (empty.length > 0) {
      setErrors(empty);
      return;
    }
    setErrors([]);
    onRespond?.(trimmed);
    setValues({});
  };

  return (
    <section className="mb-4">
      <div className="mb-2 font-mono text-mini uppercase tracking-[0.14em] text-warning">
        {hitl.taskName
          ? `awaiting human input for task: ${hitl.taskName}`
          : "awaiting human input"}
      </div>
      {responses && responses.length > 0 && (
        <div className="mt-2 space-y-1">
          {responses.map((r, i) => (
            <div
              key={i}
              className="border border-border bg-background px-3 py-1.5 font-mono text-label text-foreground"
            >
              {r}
            </div>
          ))}
        </div>
      )}
      {onRespond && (
        <form onSubmit={submit} className="mt-2.5 space-y-3">
          {fields.map((f) => {
            const isError = errors.includes(f.name);
            return (
              <div key={f.name} className="space-y-1">
                <label
                  htmlFor={`hitl-${f.name}`}
                  className="block font-mono text-micro uppercase tracking-[0.12em] text-warning"
                >
                  {f.name}
                </label>
                <div className="font-mono text-label text-muted-foreground">
                  {f.description}
                </div>
                <input
                  id={`hitl-${f.name}`}
                  value={values[f.name] ?? ""}
                  onChange={(e) => {
                    setValues((v) => ({ ...v, [f.name]: e.target.value }));
                    if (isError) setErrors((prev) => prev.filter((n) => n !== f.name));
                  }}
                  placeholder="Type your response…"
                  className={`w-full border-0 border-b bg-transparent pb-1.5 font-mono text-label text-foreground focus:border-warning ${
                    isError ? "border-destructive" : "border-border"
                  }`}
                />
                {isError && (
                  <div className="font-mono text-mini text-destructive">
                    {f.name} is required
                  </div>
                )}
              </div>
            );
          })}
          {errors.length > 0 && (
            <div className="font-mono text-mini text-destructive">
              Please fill in the required fields above.
            </div>
          )}
          <div className="flex gap-1.5">
            <Button type="submit" size="sm" className="flex-1 text-micro">
              resume
            </Button>
          </div>
        </form>
      )}
    </section>
  );
}
