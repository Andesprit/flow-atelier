import { useState, useCallback, useMemo } from "react";
import {
  DndContext,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  DragOverlay,
  type DragStartEvent,
  type DragEndEvent,
} from "@dnd-kit/core";
import { toast } from "sonner";
import { Trash2 } from "lucide-react";
import { startTask } from "@/runner/engine";
import { useStoreWithEqualityFn } from "zustand/traditional";
import { useTaskStore } from "@/runner";
import { useConduit } from "@/hooks/useConduit";
import { useRunTitle } from "@/hooks/useRunTitle";
import { useRunNotifications } from "@/hooks/useRunNotifications";
import { getConduitCached } from "@/services/conduits";

import {
  loadProjects,
  saveProjects,
  loadActiveProjectId,
  saveActiveProjectId,
} from "@/services/storage/projects";
import { KANBAN_COLUMNS } from "@/constants/kanban";
import type { Task, ColumnId } from "@/types/task";
import { TaskCard } from "./components/TaskCard";
import { TaskCardRunning } from "./components/TaskCardRunning";
import { KanbanColumn } from "./components/KanbanColumn";
import { TaskDrawer } from "./components/TaskDrawer";
import { NewTaskDialog } from "./components/NewTaskDialog";
import { NewProjectDialog } from "./components/NewProjectDialog";
import { DeleteProjectDialog } from "./components/DeleteProjectDialog";
import { DateRangePicker } from "./components/DateRangePicker";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type DatePreset = "all" | "today" | "yesterday" | "week" | "month" | "custom";

const DATE_OPTIONS: Array<{ value: DatePreset; label: string }> = [
  { value: "all", label: "All time" },
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
  { value: "week", label: "This week" },
  { value: "month", label: "This month" },
  { value: "custom", label: "Date range" },
];

function dateFilterRange(preset: DatePreset, customFrom?: string, customTo?: string): [number, number] | null {
  const now = Date.now();
  const DAY = 86_400_000;
  switch (preset) {
    case "today": {
      const start = new Date(); start.setHours(0, 0, 0, 0);
      return [start.getTime(), now + DAY];
    }
    case "yesterday": {
      const start = new Date(); start.setHours(0, 0, 0, 0);
      return [start.getTime() - DAY, start.getTime()];
    }
    case "week": {
      const d = new Date(); d.setDate(d.getDate() - d.getDay());
      d.setHours(0, 0, 0, 0);
      return [d.getTime(), d.getTime() + 7 * DAY];
    }
    case "month": {
      const d = new Date(); d.setDate(1); d.setHours(0, 0, 0, 0);
      return [d.getTime(), now + DAY];
    }
    case "custom": {
      if (!customFrom || !customTo) return null;
      const [fy, fm, fd] = customFrom.split("-").map(Number);
      const [ty, tm, td] = customTo.split("-").map(Number);
      const from = new Date(fy, fm - 1, fd, 0, 0, 0, 0);
      const to = new Date(ty, tm - 1, td, 23, 59, 59, 999);
      return [from.getTime(), to.getTime()];
    }
    default:
      return null;
  }
}

const ALL_PROJECTS = "__all__";

// Each column says what to do next rather than "— empty —", which told the
// reader nothing they could not already see.
const EMPTY_COLUMN: Record<ColumnId, string> = {
  todo: "Nothing queued. Use + add to set up a task or conduit run.",
  in_progress: "Nothing running. Drag a task here, or press run on its card, to start it.",
  done: "No finished runs yet. Completed tasks land here with their logs.",
};

const ALLOWED_DROPS: Partial<Record<ColumnId, ColumnId[]>> = {
  todo: ["in_progress"],
  done: ["todo", "in_progress"],
};

function tasksStructuralEqual(a: Task[], b: Task[]) {
  if (a.length !== b.length) return false;
  return a.every((t, i) => {
    const tb = b[i];
    return t === tb || (
      t.name === tb.name &&
      t.column === tb.column &&
      t.projectId === tb.projectId
    );
  });
}

export function Kanban() {
  const tasks = useStoreWithEqualityFn(useTaskStore, (s) => s.tasks, tasksStructuralEqual);
  const [selectedName, setSelectedName] = useState<string | undefined>();
  const [addOpen, setAddOpen] = useState(false);
  const [editTask, setEditTask] = useState<Task | undefined>();
  const [projects, setProjects] = useState(loadProjects);
  const [activeProjectId, setActiveProjectId] = useState(loadActiveProjectId);
  const [datePreset, setDatePreset] = useState<DatePreset>("all");
  const [customDateFrom, setCustomDateFrom] = useState("");
  const [customDateTo, setCustomDateTo] = useState("");
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [deleteProjectOpen, setDeleteProjectOpen] = useState(false);
  const setTasks = useTaskStore((s) => s.setTasks);

  const { run: conduitRun, cancel: conduitCancel, resume: conduitResume, answerHITL: conduitAnswerHITL, answerAgentInput: conduitAnswerAgentInput, liveRuns } = useConduit({
    onFlowStarted: (flowId, conduitName) => {
      const task = useTaskStore.getState().tasks.find(t => t.name === conduitName && t.column === "in_progress");
      if (task) {
        useTaskStore.getState().updateTask(task.name, (t) => ({
          ...t,
          flow: { ...t.flow!, flowId },
        }));
      }
    },
    onFlowComplete: (flowId) => {
      const task = useTaskStore.getState().tasks.find(t => t.flow?.flowId === flowId);
      if (task) {
        useTaskStore.getState().updateTask(task.name, (t) => ({ ...t, column: "done" }));
      }
    },
    onError: (message) => toast.error(message),
  });
  useRunTitle(liveRuns);
  useRunNotifications(liveRuns);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor),
  );

  const [activeTask, setActiveTask] = useState<Task | null>(null);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const name = event.active.data.current?.taskName as string | undefined;
    if (name) {
      const t = useTaskStore.getState().tasks.find((x) => x.name === name);
      setActiveTask(t ?? null);
    }
  }, []);

  // Single entry point for starting a task — shared by the drag handler and
  // the "run" button so both follow the exact same path (startTask +, for real
  // conduits, conduitRun). Keeping them in sync is the whole point.
  const runTaskByName = useCallback((taskName: string) => {
    const result = startTask(taskName);
    if (result?.needsConduitRun) {
      const task = useTaskStore.getState().tasks.find((t) => t.name === taskName);
      const conduit = task ? getConduitCached(task.name) : undefined;
      if (conduit && task) {
        conduitRun(conduit.name, task.inputs, task.runPath ?? "");
      }
    }
  }, [conduitRun]);

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    setActiveTask(null);
    const { active, over } = event;
    if (!over) return;

    const sourceColumn = active.data.current?.column as ColumnId;
    const targetColumn = over.id as ColumnId;
    const taskName = active.data.current?.taskName as string;

    const allowed = ALLOWED_DROPS[sourceColumn];
    if (!allowed?.includes(targetColumn)) return;

    if (targetColumn === "in_progress") {
      runTaskByName(taskName);
    } else {
      useTaskStore.getState().updateTask(taskName, (t) => ({
        ...t,
        column: targetColumn,
        flow: undefined,
      }));
    }
  }, [runTaskByName]);

  const handleDragCancel = useCallback(() => setActiveTask(null), []);

  const showAllProjects = activeProjectId === ALL_PROJECTS;
  const activeProject = useMemo(
    () => projects.find((p) => p.id === activeProjectId) ?? projects[0] ?? { id: "", name: "" },
    [projects, activeProjectId],
  );

  const handleProjectSwitch = useCallback((id: string) => {
    if (id === "__new__") {
      setNewProjectOpen(true);
      return;
    }
    setActiveProjectId(id);
    saveActiveProjectId(id);
  }, []);

  const handleCreateProject = useCallback((name: string) => {
    const newProject = { id: `proj-${Date.now()}`, name };
    const updated = [...projects, newProject];
    setProjects(updated);
    saveProjects(updated);
    setActiveProjectId(newProject.id);
    saveActiveProjectId(newProject.id);
  }, [projects]);

  const projectHasRunningTasks = tasks.some(
    (t) => t.projectId === activeProject.id && t.column === "in_progress",
  );

  const projectHasHistory = tasks.some(
    (t) => t.projectId === activeProject.id && t.flow,
  );

  const handleDeleteProject = useCallback(() => {
    const remaining = projects.filter((p) => p.id !== activeProject.id);
    setProjects(remaining);
    saveProjects(remaining);
    setTasks(tasks.filter((t) => t.projectId !== activeProject.id));
    const nextActive = remaining[0];
    if (nextActive) {
      setActiveProjectId(nextActive.id);
      saveActiveProjectId(nextActive.id);
    } else {
      setActiveProjectId(ALL_PROJECTS);
      saveActiveProjectId(ALL_PROJECTS);
    }
  }, [projects, activeProject.id, tasks, setTasks]);

  const filterRange = useMemo(() => dateFilterRange(datePreset, customDateFrom, customDateTo), [datePreset, customDateFrom, customDateTo]);

  const filterByDate = useCallback(
    (colId: ColumnId, colTasks: Task[]) => {
      if (colId === "todo" || !filterRange) return colTasks;
      const [from, to] = filterRange;
      return colTasks.filter((t) => {
        const ts = t.flow?.startedAt ?? t.createdAt;
        return ts >= from && ts <= to;
      });
    },
    [filterRange],
  );

  const handleTaskClick = useCallback((task: Task) => {
    if (task.column === "todo") {
      setEditTask(task);
      setAddOpen(true);
    } else {
      setSelectedName(task.name);
    }
  }, []);

  const closeDialog = useCallback((open: boolean) => {
    setAddOpen(open);
    if (!open) setEditTask(undefined);
  }, []);

  const projectFilter = useCallback((t: Task) => {
    if (t.projectId === "__dashboard__") return false;
    if (showAllProjects) return true;
    return t.projectId === activeProject.id;
  }, [showAllProjects, activeProject.id]);

  return (
    <div className="min-h-[calc(100dvh-3.5rem)] px-4 py-6 lg:px-10 lg:py-10">
      {/* The 52px serif title that used to sit here repeated the active nav
          item; the top bar's wordmark carries the brand voice now. */}
      <header className="mx-auto mb-6 flex max-w-[1280px] items-baseline justify-between gap-4 border-b border-border pb-4 lg:mb-8">
        <h1 className="font-mono text-label uppercase tracking-[0.14em] text-foreground">
          run a task
        </h1>
      </header>

      <div className="mx-auto max-w-[1280px]">
        {/* Toolbar: project + date dropdowns. These were native <select>s,
            which rendered OS chrome in the middle of a page where every other
            control is custom, and killed their own focus ring with
            outline-none. ui/select.tsx already existed, unused. */}
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <Select value={activeProjectId} onValueChange={handleProjectSwitch}>
            <SelectTrigger aria-label="Filter by project" className="h-9 w-auto min-w-[160px] gap-2 text-label">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_PROJECTS}>all projects</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
              ))}
              <SelectItem value="__new__">+ new project</SelectItem>
            </SelectContent>
          </Select>

          <Select value={datePreset} onValueChange={(v) => setDatePreset(v as DatePreset)}>
            <SelectTrigger aria-label="Filter by date" className="h-9 w-auto min-w-[140px] gap-2 text-label">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DATE_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>

          {datePreset === "custom" && (
            <DateRangePicker
              from={customDateFrom}
              to={customDateTo}
              onConfirm={(f, t) => {
                setCustomDateFrom(f);
                setCustomDateTo(t);
              }}
            />
          )}

          {/* Was a full-weight bordered button in the toolbar's top-right — the
              most prominent control on the page was the destructive one, while
              the primary "+ add" sits at 12px inside a column header. Demoted
              to a quiet text action; the confirm dialog is still the guardrail. */}
          <div className="ml-auto">
            {!showAllProjects && (
              <button
                type="button"
                disabled={projectHasRunningTasks}
                onClick={() => setDeleteProjectOpen(true)}
                className="flex h-9 cursor-pointer items-center gap-1.5 rounded-sm px-2 font-mono text-mini uppercase tracking-[0.12em] text-muted-foreground transition-colors hover:text-destructive focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-muted-foreground"
              >
                <Trash2 className="size-3" aria-hidden />
                delete project
              </button>
            )}
          </div>
        </div>

        <DndContext
          sensors={sensors}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
          onDragCancel={handleDragCancel}
        >
        <div className="grid gap-4 grid-cols-1 lg:grid-cols-3">
          {KANBAN_COLUMNS.map((col) => {
            let colTasks = tasks
              .filter((t) => t.column === col.id && projectFilter(t))
              .sort((a, b) => b.createdAt - a.createdAt);
            colTasks = filterByDate(col.id, colTasks);

            return (
              <KanbanColumn
                key={col.id}
                id={col.id}
                title={col.title}
                count={colTasks.length}
                onAdd={col.id === "todo" ? () => { setEditTask(undefined); setAddOpen(true); } : undefined}
              >
                {colTasks.map((t) => (
                  <CardSelector
                    key={t.name}
                    task={t}
                    selected={selectedName === t.name}
                    onClick={() => handleTaskClick(t)}
                  />
                ))}
                {colTasks.length === 0 && (
                  <p className="border border-dashed border-border px-3 py-4 text-body leading-relaxed text-muted-foreground">
                    {EMPTY_COLUMN[col.id]}
                  </p>
                )}
              </KanbanColumn>
            );
          })}
        </div>
        <DragOverlay dropAnimation={null}>
          {activeTask && (
            <div className="h-[80px] border border-primary/60 bg-card px-3 py-2.5 shadow-lg">
              <div className="min-w-0 font-mono text-data leading-snug text-foreground line-clamp-1">
                {activeTask.name}
              </div>
              {activeTask.description && (
                <div className="mt-1 text-body leading-snug text-muted-foreground line-clamp-1">
                  {activeTask.description}
                </div>
              )}
            </div>
          )}
        </DragOverlay>
        </DndContext>
      </div>

      <TaskDrawer
        taskName={selectedName}
        onClose={() => setSelectedName(undefined)}
        liveRuns={liveRuns}
        onCancelRun={conduitCancel}
        onResumeRun={conduitResume}
        onRespondToHitl={conduitAnswerHITL}
        onAnswerAgentInput={conduitAnswerAgentInput}
      />
      <NewTaskDialog open={addOpen} onOpenChange={closeDialog} editTask={editTask} onRun={runTaskByName} projectId={showAllProjects ? (projects[0]?.id ?? "default") : activeProject.id} />
      <NewProjectDialog open={newProjectOpen} onOpenChange={setNewProjectOpen} onCreate={handleCreateProject} />
      <DeleteProjectDialog
        open={deleteProjectOpen}
        onOpenChange={setDeleteProjectOpen}
        projectName={activeProject?.name ?? ""}
        hasHistory={projectHasHistory}
        onConfirm={handleDeleteProject}
      />
    </div>
  );
}

function CardSelector({
  task,
  selected,
  onClick,
}: {
  task: Task;
  selected: boolean;
  onClick: () => void;
}) {
  if (task.column === "in_progress" && task.flow) {
    return <TaskCardRunning task={task} selected={selected} onClick={onClick} />;
  }
  return <TaskCard task={task} selected={selected} onClick={onClick} />;
}
