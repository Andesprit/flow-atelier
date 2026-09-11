import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import { useTaskStore } from "@/runner";
import type { Task } from "@/types/task";
import { hintStr, inputDefault, inputHint } from "@/types/conduit";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useNewTaskDialog } from "./useNewTaskDialog";
import { FieldRow } from "./FieldRow";
import { ProjectSelector } from "./ProjectSelector";
import { ConduitFlow } from "./ConduitFlow";
import { TaskFlow } from "./TaskFlow";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editTask?: Task;
  projectId: string;
  onRun?: (taskName: string) => void;
}

export function NewTaskDialog({ open, onOpenChange, editTask, projectId, onRun }: Props) {
  const { conduits } = useConduits();
  const s = useNewTaskDialog({ open, onOpenChange, editTask, projectId, onRun });

  return (
    <Dialog open={open} onOpenChange={s.handleOpenChange}>
      <DialogContent data-testid="new-task-dialog" className="w-[calc(100%-2rem)] max-w-xl p-4 sm:p-6">
        <DialogHeader>
          <DialogTitle>
            <em className="text-primary not-italic">{s.stepTitle()}</em>
          </DialogTitle>
          <DialogDescription>{s.stepDesc()}</DialogDescription>
        </DialogHeader>

        {s.step === "pick" && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => s.setStep("conduit-select")}
              className="rounded-md border border-border px-4 py-6 text-left transition-colors hover:bg-muted/40"
            >
              <div className="font-mono text-data text-foreground">conduit</div>
              <div className="mt-1 text-label text-muted-foreground">Run an existing conduit</div>
            </button>
            <button
              type="button"
              onClick={() => s.setStep("task-nodes")}
              className="rounded-md border border-border px-4 py-6 text-left transition-colors hover:bg-muted/40"
            >
              <div className="font-mono text-data text-foreground">task</div>
              <div className="mt-1 text-label text-muted-foreground">Build with tasks</div>
            </button>
          </div>
        )}

        {(s.step === "conduit-select" || s.step === "conduit-inputs") && (
          <ConduitFlow
            step={s.step}
            conduit={s.conduit}
            selectedConduit={s.selectedConduit}
            values={s.values}
            setValues={s.setValues}
            runPath={s.runPath}
            setRunPath={s.setRunPath}
            selectedProjectId={s.selectedProjectId}
            setSelectedProjectId={s.setSelectedProjectId}
            projects={s.projects}
            selectConduitAndAdvance={s.selectConduitAndAdvance}
          />
        )}

        {(s.step === "task-nodes" || s.step === "node-detail") && (
          <TaskFlow
            step={s.step}
            nodes={s.nodes}
            setNodes={s.setNodes}
            nodeForm={s.nodeForm}
            setNodeForm={s.setNodeForm}
            runPath={s.runPath}
            setRunPath={s.setRunPath}
            selectedProjectId={s.selectedProjectId}
            setSelectedProjectId={s.setSelectedProjectId}
            projects={s.projects}
            openNodeForm={s.openNodeForm}
            editNode={s.editNode}
            fieldErrors={s.fieldErrors}
          />
        )}

        {s.step === "run-task" && editTask && (() => {
          const editConduit = getConduitSync(editTask.name, conduits);
          const isCustom = !editConduit;
          const editInputs = editConduit?.inputs ?? {};
          return (
            <div className="space-y-3">
              <ProjectSelector
                projects={s.projects}
                selectedId={s.selectedProjectId}
                onChange={s.setSelectedProjectId}
                readOnly
                noHint
              />
              {isCustom && editTask.tool && (
                <FieldRow label="tool">
                  <div className="pb-2 font-mono text-data text-foreground">
                    {editTask.tool}
                  </div>
                </FieldRow>
              )}
              <div className="border-b border-border/60 pb-2 font-mono text-label uppercase tracking-[0.12em] text-primary">
                run path
              </div>
              <FieldRow label="Working Directory" hint="execution path" error={s.fieldErrors.runPath}>
                <input
                  value={s.runPath}
                  onChange={(e) => s.setRunPath(e.target.value)}
                  placeholder="/home/runner/..."
                  className="w-full border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground focus:border-primary"
                />
              </FieldRow>
              {!isCustom && Object.keys(editInputs).length > 0 && (
                <>
                  <div className="border-b border-border/60 pb-2 font-mono text-label uppercase tracking-[0.12em] text-primary">
                    inputs
                  </div>
                  {Object.entries(editInputs).map(([name, hint]) => (
                    <FieldRow key={name} label={name} hint={inputHint(hint)} error={s.fieldErrors[name]}>
                      <input
                        value={s.values[name] ?? ""}
                        onChange={(e) => s.setValues((v) => ({ ...v, [name]: e.target.value }))}
                        placeholder={inputDefault(hint) ?? hintStr(hint)}
                        className="w-full border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground focus:border-primary"
                      />
                    </FieldRow>
                  ))}
                </>
              )}
              {isCustom && (
                <>
                  <div className="border-b border-border/60 pb-2 font-mono text-label uppercase tracking-[0.12em] text-primary">
                    task / prompt
                  </div>
                  <FieldRow label="task" hint="Command or prompt to run" error={s.fieldErrors.runPrompt}>
                    <textarea
                      value={s.runPrompt}
                      onChange={(e) => s.setRunPrompt(e.target.value)}
                      placeholder="Command or prompt"
                      rows={3}
                      className="w-full resize-none border border-border/60 bg-transparent p-2 font-mono text-label text-foreground focus:border-primary"
                    />
                  </FieldRow>
                </>
              )}
            </div>
          );
        })()}

        <DialogFooter>
          {s.step !== "pick" && s.step !== "run-task" && (
            <Button type="button" variant="ghost" size="sm" onClick={s.backStep}>
              ← back
            </Button>
          )}
          {s.step === "run-task" && editTask && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid="ntd-remove"
              onClick={() => {
                useTaskStore.getState().remove(editTask.name);
                onOpenChange(false);
              }}
              className="mr-auto text-muted-foreground hover:text-destructive"
            >
              remove
            </Button>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            cancel
          </Button>
          {s.step === "conduit-inputs" && (
            <Button type="button" size="sm" data-testid="ntd-submit" onClick={s.submitConduit}>
              add conduit
            </Button>
          )}
          {s.step === "task-nodes" && (
            <span className="font-mono text-mini text-muted-foreground">
              select a tool to add a node
            </span>
          )}
          {s.step === "run-task" && (
            <Button type="button" size="sm" data-testid="ntd-submit" onClick={s.runTask}>
              run
            </Button>
          )}
          {s.step === "node-detail" && (
            <Button type="button" size="sm" onClick={s.saveNode}>
              create task
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
