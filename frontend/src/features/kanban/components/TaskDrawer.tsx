import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { useTaskStore, cancelTask as cancelEngine, resumeWithAnswers } from "@/runner";
import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import { fetchFlowLogs } from "@/services/conduits";
import { openPath } from "@/services/api/conduits";
import { FlowDrawer, computeTaskDurations, buildChildRunsFromLogs, statusFromMarkers } from "@/components/FlowDrawer";
import type { FlowDrawerTask } from "@/components/FlowDrawer";
import type { LogEntry } from "@/types/task";
import type { LiveRun } from "@/hooks/useConduit";

interface Props {
  taskName: string | undefined;
  onClose: () => void;
  liveRuns?: LiveRun[];
  onCancelRun?: (flowId: string) => void;
  onResumeRun?: (flowId: string, conduitName?: string) => void;
  onRespondToHitl?: (flowId: string, answers: Record<string, string>) => void;
  onAnswerAgentInput?: (flowId: string, requestId: string, answer: string) => void;
}

export function TaskDrawer({ taskName, onClose, liveRuns = [], onCancelRun, onResumeRun, onRespondToHitl, onAnswerAgentInput }: Props) {
  const { conduits } = useConduits();
  const task = useTaskStore((s) =>
    taskName ? s.tasks.find((t) => t.name === taskName) : undefined,
  );

  const flowId = task?.flow?.flowId;
  const isLive = task?.column === "in_progress";

  // Match a LiveRun for this task (real conduit mode).
  // For mock mode, liveRun will be undefined — falls back to task.flow.
  const liveRun = useMemo(
    () => (flowId && isLive) ? liveRuns.find(r => r.flowId === flowId) : undefined,
    [liveRuns, flowId, isLive],
  );

  // Live child runs (real conduit sub-conduits)
  const liveChildRuns = useMemo(
    () => liveRun ? liveRuns.filter(r => r.parentFlowId === liveRun.flowId) : [],
    [liveRuns, liveRun],
  );

  // Fetch historical logs from API for completed tasks (matching dashboard pattern:
  // running → store/WebSocket, not running → API for complete data).
  const [flowLogs, setFlowLogs] = useState<LogEntry[]>([]);
  const [priorTasks, setPriorTasks] = useState<FlowDrawerTask[]>([]);

  useEffect(() => {
    if (flowId && !isLive) {
      fetchFlowLogs(flowId)
        .then(({ logs, tasks }) => {
          setFlowLogs(logs);
          setPriorTasks(tasks as FlowDrawerTask[]);
        })
        .catch(() => toast.error("Failed to load flow logs"));
    } else {
      setFlowLogs([]);
      setPriorTasks([]);
    }
  }, [flowId, isLive]);

  const conduit = taskName ? getConduitSync(taskName, conduits) : undefined;
  const flow = task?.flow;

  // Reconstruct nested child runs from parent's flat log entries + conduit defs
  const priorChildRuns = useMemo<LiveRun[]>(() => {
    if (isLive) return [];
    return buildChildRunsFromLogs(flowLogs, conduit, conduits, flowId ?? "");
  }, [isLive, flowLogs, conduit, conduits, flowId]);

  // ── Data source selection ──────────────────────────────────────────────────
  //
  // 1. Live + LiveRun → real conduit, read from LiveRun
  // 2. Live + no LiveRun → mock mode, read from task.flow
  // 3. Not live → prior flow, fetch from API

  const drawerHitl = liveRun?.hitlRequest ?? flow?.hitlRequest;

  // Includes child-flow turns: a nested conduit's interactive task prompts under
  // its own flow id, which never matches `liveRun`, so leaving them out means the
  // reply box is never drawn and that agent stays parked until cancelled.
  const agentInputs = useMemo(
    () => [
      ...(liveRun?.agentRequests ?? []),
      ...liveChildRuns.flatMap((c) => c.agentRequests),
    ],
    [liveRun, liveChildRuns],
  );

  const tasks = useMemo<FlowDrawerTask[] | undefined>(() => {
    // Real conduit live run
    if (liveRun) {
      const taskNames = (conduit?.tasks ?? []).map((st) => st.name);
      const durations = computeTaskDurations(liveRun.logLines, taskNames);
      return (conduit?.tasks ?? []).map((st) => {
        const child = liveChildRuns.find(c => c.parentTask === st.name);
        return {
          name: st.name,
          status: liveRun.taskStatuses[st.name] ?? "pending",
          tool: st.tool,
          childFlowId: child?.flowId,
          durationMs: durations.get(st.name),
        };
      });
    }

    // Mock live
    if (isLive && flow) {
      const taskNames = (conduit?.tasks ?? []).map((st) => st.name);
      const durations = computeTaskDurations(flow.logLines, taskNames);
      return (conduit?.tasks ?? []).map((st) => ({
        name: st.name,
        status: flow.taskStatuses[st.name] ?? "pending",
        durationMs: durations.get(st.name),
      }));
    }

    // Prior flow: use conduit definition for nested structure
    if (!isLive && conduit && flowLogs.length > 0) {
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

    return priorTasks.length > 0 ? priorTasks : undefined;
  }, [liveRun, conduit, liveChildRuns, isLive, flow, flowLogs, priorChildRuns, priorTasks]);

  const subtitle = conduit
    ? `${conduit.name} · ${conduit.tasks.length} tasks`
    : task?.description;

  // Logs: store (WebSocket) for running tasks, API for completed tasks.
  const logLines = liveRun?.logLines
    ?? (isLive ? flow?.logLines : undefined)
    ?? (flowLogs.length > 0 ? flowLogs : flow?.logLines);

  if (!taskName || !task) return null;

  const allChildRuns = liveChildRuns.length > 0 ? liveChildRuns : priorChildRuns;

  return (
    <FlowDrawer
      open={!!taskName}
      onClose={onClose}
      flowId={flowId}
      title={task.name}
      subtitle={subtitle}
      badge={task.column.replace("_", " ")}
      tasks={tasks}
      logLines={logLines}
      startedAt={liveRun?.startedAt ?? flow?.startedAt}
      hitl={drawerHitl}
      onRespondToHitl={
        drawerHitl && onRespondToHitl && liveRun
          ? (answers) => onRespondToHitl(liveRun.flowId, answers)
          : drawerHitl
            ? (answers) => resumeWithAnswers(task.name, answers)
            : undefined
      }
      agentInputs={agentInputs}
      onAnswerAgentInput={
        onAnswerAgentInput
          ? (request, answer) =>
              onAnswerAgentInput(request.flowId, request.requestId, answer)
          : undefined
      }
      onRemove={
        task.column === "todo"
          ? () => { useTaskStore.getState().remove(task.name); }
          : undefined
      }
      onOpenPath={
        task.runPath
          ? () => openPath(task.name, task.runPath!)
          : undefined
      }
      onCancel={
        onCancelRun && liveRun && liveRun.status === "running"
          ? () => onCancelRun(liveRun.flowId)
          : task.column !== "done" && !liveRun
            ? () => cancelEngine(task.name)
            : undefined
      }
      hideCancel={!liveRun || liveRun.status !== "running"}
      onResume={
        onResumeRun
          ? liveRun && (liveRun.status === "cancelled" || liveRun.status === "failed")
            ? () => onResumeRun(liveRun.flowId)
            : !liveRun && task.column === "done" && flowId
              ? () => onResumeRun(flowId)
              : undefined
          : undefined
      }
      inputs={task.inputs}
      runPath={task.runPath}
      childRuns={allChildRuns.length > 0 ? allChildRuns : undefined}
    />
  );
}
