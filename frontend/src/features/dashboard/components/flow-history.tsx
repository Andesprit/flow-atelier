import { useState, useCallback, useMemo, useEffect } from "react";
import { toast } from "sonner";
import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import { fetchFlows, fetchFlowLogs } from "@/services/conduits";
import { openPath } from "@/services/api/conduits";
import { HistRow, type Row } from "./hist-row";
import { ScheduledJobs } from "./scheduled-jobs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { FlowDrawer, type FlowDrawerTask, computeTaskDurations, buildChildRunsFromLogs, statusFromMarkers } from "@/components/FlowDrawer";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { FLOW_HISTORY_SCROLL_THRESHOLD } from "@/constants/dashboard";
import { cn } from "@/lib/cn";
import type { ScheduledJob } from "@/types/schedule";
import type { LogEntry } from "@/types/task";
import type { PriorFlow } from "@/types/flow";
import { waitingFlowIds, type LiveRun } from "@/hooks/useConduit";

type SortCol = "flow" | "duration" | "started" | "state";

type Tab = "flows" | "scheduled";

interface Props {
  scheduledJobs?: ScheduledJob[];
  onDeleteJob?: (id: string) => void;
  onAddSchedule?: () => void;
  refreshKey?: number;
  /** Flow to reveal as soon as it starts, so a run is watchable immediately. */
  autoOpenFlowId?: string;
  /** Live runs from the current dashboard session. */
  liveRuns: LiveRun[];
  onRespondToHitl?: (flowId: string, answers: Record<string, string>) => void;
  onAnswerAgentInput?: (flowId: string, requestId: string, answer: string) => void;
  onCancelRun?: (flowId: string) => void;
  onResumeRun?: (flowId: string, conduitName?: string) => void;
}

export function FlowHistory({
  scheduledJobs = [],
  onDeleteJob,
  onAddSchedule,
  refreshKey,
  autoOpenFlowId,
  liveRuns,
  onRespondToHitl,
  onAnswerAgentInput,
  onCancelRun,
  onResumeRun,
}: Props) {
  const [activeTab, setActiveTab] = useState<Tab>("flows");
  const [selectedFlowId, setSelectedFlowId] = useState<string | undefined>();
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  const [pendingCancelFlowId, setPendingCancelFlowId] = useState<string | undefined>();
  const [priorFlows, setPriorFlows] = useState<PriorFlow[]>([]);
  const [sort, setSort] = useState<{ col: SortCol; asc: boolean }>({ col: "started", asc: false });
  const { conduits } = useConduits();
  const rowRef = useCallback((node: HTMLLIElement | null) => {
    if (node) setRowHeight(node.offsetHeight);
  }, []);
  const [rowHeight, setRowHeight] = useState(0);

  // Reveal a freshly started run without stealing focus from a drawer the user
  // already opened. Keyed on the id, so re-renders never re-open a closed one.
  useEffect(() => {
    if (autoOpenFlowId) {
      setActiveTab("flows");
      setSelectedFlowId(autoOpenFlowId);
    }
  }, [autoOpenFlowId]);

  useEffect(() => {
    let ignore = false;
    fetchFlows()
      .then((flows) => {
        if (!ignore) setPriorFlows(flows);
      })
      .catch(() => {
        if (!ignore) toast.error("Failed to load flows");
      });
    return () => {
      ignore = true;
    };
  }, [refreshKey]);

  // ── Row list ──────────────────────────────────────────────────────────────

  const waiting = useMemo(() => waitingFlowIds(liveRuns), [liveRuns]);

  const liveRows: Row[] = useMemo(
    () =>
      liveRuns
        .filter((r) => !r.parentFlowId)
        .map((r) => ({
          flowId: r.flowId,
          conduit: r.conduitName,
          startedAt: r.startedAt,
          duration:
            r.status === "running"
              ? Date.now() - r.startedAt
              : (r.logLines[r.logLines.length - 1]?.t ?? Date.now()) - r.startedAt,
          state: r.status,
          tag:
            r.status === "running"
              ? waiting.has(r.flowId) ? "waiting" : "live"
              : r.status === "cancelled" ? "cancelled" : r.status,
          isConduit: true,
        })),
    [liveRuns, waiting],
  );

  // Dedup: exclude prior rows whose flowId matches a live run (exact match only).
  const liveFlowIds = useMemo(
    () => new Set(liveRuns.map((r) => r.flowId)),
    [liveRuns],
  );

  const priorRows: Row[] = useMemo(
    () =>
      priorFlows
        .filter((p) => !liveFlowIds.has(p.flowId))
        .map((p) => {
          const isTask = p.conduitName.startsWith("task__");
          return {
            flowId: p.flowId,
            conduit: isTask ? p.conduitName.slice(6) : p.conduitName,
            startedAt: p.startedAt,
            duration: p.duration ?? 0,
            state: p.status === "cancelled" ? "cancelled" : p.status,
            tag: p.status,
            isConduit: !isTask,
          };
        }),
    [priorFlows, liveFlowIds],
  );

  const rows = useMemo(() => {
    const sorted = [...liveRows, ...priorRows]
      .sort((a, b) => {
        const dir = sort.asc ? 1 : -1;
        switch (sort.col) {
          case "flow":
            return dir * a.conduit.localeCompare(b.conduit);
          case "duration":
            return dir * ((a.duration ?? 0) - (b.duration ?? 0));
          case "started":
            return dir * (a.startedAt - b.startedAt);
          case "state":
            return dir * a.tag.localeCompare(b.tag);
        }
      });
    return sorted;
  }, [liveRows, priorRows, sort]);

  const toggleSort = (col: SortCol) =>
    setSort((s) => (s.col === col ? { col, asc: !s.asc } : { col, asc: true }));

  const constrainedHeight =
    rows.length > FLOW_HISTORY_SCROLL_THRESHOLD && rowHeight > 0
      ? rowHeight * FLOW_HISTORY_SCROLL_THRESHOLD
      : undefined;

  const selectedRow = rows.find((r) => r.flowId === selectedFlowId);

  // ── Drawer data source ────────────────────────────────────────────────────

  // Selected live run (or undefined if it's a historical flow).
  // No sticky name needed — liveRuns persists the data until evicted.
  const selectedLiveRun = useMemo(
    () => (selectedFlowId ? liveRuns.find((r) => r.flowId === selectedFlowId) : undefined),
    [liveRuns, selectedFlowId],
  );

  const priorFlow = selectedFlowId
    ? priorFlows.find((p) => p.flowId === selectedFlowId)
    : undefined;

  const [flowLogs, setFlowLogs] = useState<LogEntry[]>([]);
  const [priorTasks, setPriorTasks] = useState<FlowDrawerTask[]>([]);
  const [priorRunPath, setPriorRunPath] = useState<string | undefined>();

  useEffect(() => {
    if (selectedFlowId && !selectedLiveRun) {
      let ignore = false;
      fetchFlowLogs(selectedFlowId)
        .then(({ logs, tasks, runPath }) => {
          if (ignore) return;
          setFlowLogs(logs);
          setPriorTasks(tasks as FlowDrawerTask[]);
          setPriorRunPath(runPath);
        })
        .catch(() => {
          if (!ignore) toast.error("Failed to load flow logs");
        });
      return () => {
        ignore = true;
      };
    } else {
      setFlowLogs([]);
      setPriorTasks([]);
      setPriorRunPath(undefined);
    }
  }, [selectedFlowId, selectedLiveRun]);

  const childRuns = useMemo(
    () => selectedFlowId ? liveRuns.filter(r => r.parentFlowId === selectedFlowId) : [],
    [liveRuns, selectedFlowId],
  );

  // Reconstruct nested child runs from parent's flat log entries + conduit defs
  const priorChildRuns = useMemo<LiveRun[]>(() => {
    if (selectedLiveRun) return [];
    const parentDef = priorFlow
      ? getConduitSync(priorFlow.conduitName, conduits)
      : undefined;
    return buildChildRunsFromLogs(flowLogs, parentDef, conduits, selectedFlowId ?? "");
  }, [selectedLiveRun, priorFlow, conduits, flowLogs, selectedFlowId]);

  const drawerTasks = useMemo<FlowDrawerTask[] | undefined>(() => {
    if (selectedLiveRun) {
      const conduit = getConduitSync(selectedLiveRun.conduitName, conduits);
      const taskNames = (conduit?.tasks ?? []).map((st) => st.name);
      const durations = computeTaskDurations(selectedLiveRun.logLines, taskNames);
      return (conduit?.tasks ?? []).map((st) => {
        const child = childRuns.find(c => c.parentTask === st.name);
        return {
          name: st.name,
          status: selectedLiveRun.taskStatuses[st.name] ?? "pending",
          tool: st.tool,
          childFlowId: child?.flowId,
          durationMs: durations.get(st.name),
        };
      });
    }

    // Prior flow: use conduit definition for nested structure
    if (priorFlow) {
      const conduit = getConduitSync(priorFlow.conduitName, conduits);
      if (conduit && conduit.tasks.length > 0) {
        const taskNames = conduit.tasks.map((st) => st.name);
        const durations = computeTaskDurations(flowLogs, taskNames);
        const childByParentTask = new Map<string, string>();
        for (const cr of priorChildRuns) {
          if (cr.parentTask) childByParentTask.set(cr.parentTask, cr.flowId);
        }
        return conduit.tasks.map((st) => ({
          name: st.name,
          status: statusFromMarkers(flowLogs, st.name, true),
          tool: st.tool,
          childFlowId: childByParentTask.get(st.name),
          durationMs: durations.get(st.name),
        }));
      }
    }

    return priorTasks.length > 0 ? priorTasks : undefined;
  }, [selectedLiveRun, conduits, priorTasks, childRuns, priorFlow, flowLogs, priorChildRuns]);

  const priorFlowHitl = useMemo(() => {
    if (!priorFlow || priorFlow.status !== "done") return undefined;
    const hasReviewGate = flowLogs.some(
      (l) => l.text.includes("review_gate") || l.text.includes("awaiting human"),
    );
    if (!hasReviewGate) return undefined;
    return {
      fromTool: "harness:claude-code" as const,
      comment: "Review the generated output before proceeding.",
    };
  }, [priorFlow, flowLogs]);

  const drawerLogs = selectedLiveRun?.logLines
    ?? (selectedFlowId ? flowLogs : undefined);

  const drawerStartedAt = selectedLiveRun?.startedAt ?? priorFlow?.startedAt;
  const drawerHitl = selectedLiveRun?.hitlRequest ?? priorFlowHitl;
  const drawerInputCount = selectedLiveRun
    ? Object.keys(selectedLiveRun.inputs).length
    : 0;
  const drawerHideCancel = !selectedLiveRun || selectedLiveRun.status !== "running";

  // A nested conduit's interactive task prompts under its own child flow id, so
  // its request never lands on the selected run. Without the child requests here
  // the reply box is never drawn and that agent stays parked until cancelled.
  const drawerAgentInputs = useMemo(
    () => [
      ...(selectedLiveRun?.agentRequests ?? []),
      ...childRuns.flatMap((c) => c.agentRequests),
    ],
    [selectedLiveRun, childRuns],
  );

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleRowClick = (row: Row) => {
    setSelectedFlowId(row.flowId);
  };

  const handleDrawerClose = () => {
    setSelectedFlowId(undefined);
  };

  return (
    <aside data-testid="flow-history">
      <div className="mb-5 flex items-baseline gap-0 border-b border-border">
        {(
          [
            { id: "flows" as Tab, label: `recent flows · ${rows.length}` },
            { id: "scheduled" as Tab, label: `scheduled · ${scheduledJobs.length}` },
          ] as const
        ).map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              "mr-6 pb-2 font-mono text-label uppercase tracking-[0.12em] transition-colors border-b-2 -mb-px",
              activeTab === tab.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            · {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "flows" ? (
        rows.length === 0 ? (
          <div className="py-12 text-center">
            <div className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
              no flows yet
            </div>
            <div className="mt-2 font-mono text-label text-muted-foreground">
              run a conduit to see it here
            </div>
          </div>
        ) : (
        <>
          <div className="grid grid-cols-[14px_1fr_90px_80px_60px] items-center gap-4 border-b border-border pr-5 font-mono text-mini uppercase tracking-[0.14em] text-muted-foreground">
            <span />
            {([
              { col: "flow" as SortCol, label: "flow", align: "" },
              { col: "duration" as SortCol, label: "duration", align: "" },
              { col: "started" as SortCol, label: "started", align: "" },
              { col: "state" as SortCol, label: "state", align: "justify-end" },
            ]).map(({ col, label, align }) => (
              <button
                key={col}
                type="button"
                onClick={() => toggleSort(col)}
                aria-label={`Sort by ${label}`}
                // h-9: the sort targets were 15px tall, under the 24px floor.
                className={cn(
                  "flex h-9 items-center gap-1 transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary",
                  sort.col === col && "text-foreground",
                  align,
                )}
              >
                {label}
                <span className="inline-block w-2 text-micro">
                  {sort.col === col ? (sort.asc ? "▲" : "▼") : ""}
                </span>
              </button>
            ))}
          </div>
          <ScrollArea style={constrainedHeight ? { height: constrainedHeight } : undefined}>
            <ul className="divide-y divide-border/60">
              {rows.map((row, i) => (
                <HistRow
                  key={row.flowId}
                  row={row}
                  ref={i === 0 ? rowRef : undefined}
                  onClick={() => handleRowClick(row)}
                />
              ))}
            </ul>
          </ScrollArea>
        </>
      )) : (
        <ScheduledJobs jobs={scheduledJobs} onDelete={onDeleteJob} onAdd={onAddSchedule} />
      )}

      <FlowDrawer
        open={!!selectedFlowId}
        onClose={handleDrawerClose}
        flowId={selectedFlowId}
        title={selectedRow?.conduit ?? priorFlow?.conduitName ?? ""}
        subtitle={
          selectedLiveRun
            ? (() => {
                const c = getConduitSync(selectedLiveRun.conduitName, conduits);
                return c
                  ? `${selectedLiveRun.conduitName} · ${c.tasks.length} tasks`
                  : selectedLiveRun.conduitName;
              })()
            : priorFlow
              ? `${priorFlow.conduitName}${priorFlow.author ? ` · ${priorFlow.author}` : ""}`
              : undefined
        }
        badge={
          selectedLiveRun
            ? selectedLiveRun.status === "running"
              ? waiting.has(selectedLiveRun.flowId) ? "waiting" : "live"
              : selectedLiveRun.status === "cancelled"
                ? "cancelled"
                : selectedLiveRun.status
            : selectedRow?.tag ?? undefined
        }
        tasks={drawerTasks}
        logLines={drawerLogs}
        startedAt={drawerStartedAt}
        duration={priorFlow?.duration}
        hitl={drawerHitl}
        onRespondToHitl={
          onRespondToHitl && selectedLiveRun?.hitlRequest
            ? (answers) => onRespondToHitl(selectedLiveRun.flowId, answers)
            : undefined
        }
        agentInputs={drawerAgentInputs}
        onAnswerAgentInput={
          onAnswerAgentInput
            ? (request, answer) =>
                onAnswerAgentInput(request.flowId, request.requestId, answer)
            : undefined
        }
        inputCount={drawerInputCount}
        onOpenPath={
          selectedLiveRun?.runPath
            ? () => openPath(selectedLiveRun.conduitName, selectedLiveRun.runPath)
            : priorRunPath
              ? () => openPath(selectedFlowId ?? "", priorRunPath)
              : undefined
        }
        onCancel={
          selectedLiveRun && onCancelRun
            ? () => {
                setPendingCancelFlowId(selectedLiveRun.flowId);
                setCancelConfirmOpen(true);
              }
            : undefined
        }
        hideCancel={drawerHideCancel}
        onResume={
          onResumeRun
            ? selectedLiveRun && (selectedLiveRun.status === "cancelled" || selectedLiveRun.status === "failed")
              ? () => onResumeRun(selectedLiveRun.flowId)
              : !selectedLiveRun && (priorFlow?.status === "cancelled" || priorFlow?.status === "failed") && selectedFlowId
                ? () => onResumeRun(selectedFlowId, priorFlow.conduitName)
                : undefined
            : undefined
        }
        childRuns={childRuns.length > 0 ? childRuns : priorChildRuns}
      />

      <Dialog open={cancelConfirmOpen} onOpenChange={setCancelConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel run?</DialogTitle>
            <DialogDescription>
              This will stop the running conduit. You can resume it later.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setCancelConfirmOpen(false)}
            >
              keep running
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="border-destructive/50 text-destructive hover:bg-destructive/10 hover:text-destructive"
              onClick={() => {
                if (pendingCancelFlowId && onCancelRun) {
                  onCancelRun(pendingCancelFlowId);
                }
                setCancelConfirmOpen(false);
                setPendingCancelFlowId(undefined);
                handleDrawerClose();
              }}
            >
              cancel run
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </aside>
  );
}
