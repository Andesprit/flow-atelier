import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from "react";
import { toast } from "sonner";
import { useConduit, type LiveRun } from "@/hooks/useConduit";
import { useRunTitle } from "@/hooks/useRunTitle";
import { useRunNotifications } from "@/hooks/useRunNotifications";
import { useTaskStore } from "@/runner";

/** The screen a run was started from; each keeps its own socket and its own effects. */
export type RunOrigin = "dashboard" | "kanban";

type Session = ReturnType<typeof useConduit>;

/** Router state asking a screen to show one run's controls. Display intent only. */
export interface OpenRunState {
  openFlowId: string;
}

export function openRunState(state: unknown): string | undefined {
  const id = (state as Partial<OpenRunState> | null)?.openFlowId;
  return typeof id === "string" && id ? id : undefined;
}


/** Effects that only make sense while the dashboard is on screen. */
export interface DashboardEffects {
  onFlowStarted?: (flowId: string) => void;
  onFlowComplete?: (flowId: string) => void;
}

interface LiveRunsValue {
  dashboard: Session;
  kanban: Session;
  dashboardEffects: { current: DashboardEffects };
}

const LiveRunsContext = createContext<LiveRunsValue | null>(null);

function useLiveRunsContext(): LiveRunsValue {
  const value = useContext(LiveRunsContext);
  if (!value) throw new Error("LiveRunsProvider is missing");
  return value;
}

/**
 * Owns the dashboard's and the kanban board's run sessions for the life of the
 * tab, above the routes. The server cancels every run on a socket the moment it
 * closes, so a session that lived inside its screen stopped the work as soon as
 * the person clicked to another screen. Closing or reloading the tab still
 * closes both sockets, and the leave guard in useConduit asks first.
 */
export function LiveRunsProvider({ children }: { children: ReactNode }) {
  const dashboardEffects = useRef<DashboardEffects>({});
  const dashboard = useConduit({
    // Routed through a ref the dashboard fills while mounted: a start or a
    // finish arriving while it is away must not open a drawer on another screen.
    onFlowStarted: (flowId) => dashboardEffects.current.onFlowStarted?.(flowId),
    onFlowComplete: (flowId) => dashboardEffects.current.onFlowComplete?.(flowId),
    onError: (message) => toast.error(message),
  });
  // The board's effects only touch the task store, so they stay here and keep
  // its cards right while the board is not mounted.
  const kanban = useConduit({
    onFlowStarted: (flowId, conduitName) => {
      const task = useTaskStore.getState().tasks.find((t) => t.name === conduitName && t.column === "in_progress");
      if (task) {
        useTaskStore.getState().updateTask(task.name, (t) => ({
          ...t,
          flow: { ...t.flow!, flowId },
        }));
      }
    },
    onFlowComplete: (flowId) => {
      const task = useTaskStore.getState().tasks.find((t) => t.flow?.flowId === flowId);
      if (task) {
        useTaskStore.getState().updateTask(task.name, (t) => ({ ...t, column: "done" }));
      }
    },
    onError: (message) => toast.error(message),
  });

  const allRuns = useMemo(
    () => [...dashboard.liveRuns, ...kanban.liveRuns].sort((a, b) => a.startedAt - b.startedAt),
    [dashboard.liveRuns, kanban.liveRuns],
  );
  useRunTitle(allRuns);
  useRunNotifications(allRuns);

  const value = useMemo(
    () => ({ dashboard, kanban, dashboardEffects }),
    [dashboard, kanban],
  );
  return <LiveRunsContext.Provider value={value}>{children}</LiveRunsContext.Provider>;
}

/** The dashboard's session; its screen effects apply only while it is mounted. */
export function useDashboardRuns(effects: DashboardEffects): Session {
  const { dashboard, dashboardEffects } = useLiveRunsContext();
  const latest = useRef(effects);
  latest.current = effects;
  useEffect(() => {
    dashboardEffects.current = {
      onFlowStarted: (flowId) => latest.current.onFlowStarted?.(flowId),
      onFlowComplete: (flowId) => latest.current.onFlowComplete?.(flowId),
    };
    return () => {
      dashboardEffects.current = {};
    };
  }, [dashboardEffects]);
  return dashboard;
}

export function useKanbanRuns(): Session {
  return useLiveRunsContext().kanban;
}

/** Every run of both sessions, tagged with the screen that owns its controls. */
export function useAllLiveRuns(): { origin: RunOrigin; run: LiveRun }[] {
  const { dashboard, kanban } = useLiveRunsContext();
  return useMemo(
    () => [
      ...dashboard.liveRuns.map((run) => ({ origin: "dashboard" as const, run })),
      ...kanban.liveRuns.map((run) => ({ origin: "kanban" as const, run })),
    ],
    [dashboard.liveRuns, kanban.liveRuns],
  );
}
