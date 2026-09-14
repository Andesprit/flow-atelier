import { useState, useCallback, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import { createConduit, updateConduit } from "@/services/api/conduits";
import {
  loadDraftConduit,
  saveDraftConduit,
  clearDraftConduit,
} from "@/services/storage/draft-conduit";
import type { Conduit, ConduitTask } from "@/types/conduit";
import { escapeRegExp } from "@/utils/regex";
import { Canvas } from "./components/Canvas";
import { ToolPanel } from "./components/ToolPanel";
import { Inspector } from "./components/Inspector";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogTrigger,
  DialogClose,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { renderConduitYaml } from "@/utils/yaml";
import { useUndoState } from "@/hooks/useUndoState";
import { useIsMobile } from "@/hooks/useIsMobile";
import { renameConditionSource, toWireTasks } from "@/utils/conditions";
import { Menu, Undo2, Redo2, Play } from "lucide-react";

import { interactionError } from "./interaction-policy";

const MENU_ITEM =
  "flex h-11 w-full items-center rounded-sm px-3 text-left font-mono text-label text-foreground hover:bg-muted";

function initConduit(conduits: Conduit[]): Conduit {
  return (
    loadDraftConduit() ??
    structuredClone(getConduitSync("deploy_pipeline", conduits) ?? {
      name: "new_conduit",
      description: "",
      inputs: {},
      tasks: [],
    })
  );
}

export function Designer() {
  const { conduits: allConduits, refresh } = useConduits();
  const [conduit, setConduitRaw, undo, redo] = useUndoState<Conduit>(() => initConduit(allConduits));
  const [saving, setSaving] = useState(false);

  const setConduit = useCallback(
    (value: Conduit | ((prev: Conduit) => Conduit)) => {
      setConduitRaw((prev) => {
        const next =
          typeof value === "function"
            ? (value as (p: Conduit) => Conduit)(prev)
            : value;
        saveDraftConduit(next);
        return next;
      });
    },
    [setConduitRaw],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if ((mod && e.key === "y") || (mod && e.shiftKey && e.key === "z")) {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  const [selectedName, setSelectedName] = useState<string | undefined>();
  const [yamlOpen, setYamlOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createTimeout, setCreateTimeout] = useState("3600");
  const [createMaxConcurrency, setCreateMaxConcurrency] = useState("1");
  const positionRef = useRef<((tool: string) => { x: number; y: number }) | null>(null);
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const [toolPanelOpen, setToolPanelOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  const updateTask = useCallback(
    (name: string, partial: Partial<ConduitTask>) => {
      const newName = partial.name && partial.name !== name ? partial.name : null;
      setConduit((prev) => ({
        ...prev,
        tasks: prev.tasks.map((t) => {
          if (t.name === name) return { ...t, ...partial };
          if (!newName) return t;
          return {
            ...t,
            dependsOn: t.dependsOn.map((d) => (d === name ? newName : d)),
            conditions: renameConditionSource(t.conditions, name, newName),
          };
        }),
      }));
      if (newName) setSelectedName(newName);
    },
    [setConduit],
  );

  const deleteTask = useCallback((name: string) => {
    setConduit((prev) => ({
      ...prev,
      tasks: prev.tasks
        .filter((t) => t.name !== name)
        .map((t) => ({
          ...t,
          dependsOn: t.dependsOn.filter((d) => d !== name),
        })),
    }));
    setSelectedName((prev) => (prev === name ? undefined : prev));
  }, [setConduit]);

  const addTask = useCallback((task: ConduitTask) => {
    const pos = positionRef.current?.(task.tool) ?? task.position ?? { x: 80, y: 140 };
    setConduit((prev) => {
      // Generate a unique temp name if empty to avoid id collisions in ReactFlow
      const name = task.name || (() => {
        const existing = new Set(prev.tasks.map((t) => t.name));
        let n = 1;
        while (existing.has(`task_${n}`)) n++;
        return `task_${n}`;
      })();
      const positioned = { ...task, name, position: pos };
      setSelectedName(name);
      return {
        ...prev,
        tasks: [...prev.tasks, positioned],
      };
    });
  }, [setConduit]);

  const addInput = useCallback(
    (name: string, hint: string) => {
      setConduit((prev) => ({
        ...prev,
        inputs: { ...prev.inputs, [name]: hint },
      }));
    },
    [setConduit],
  );

  const removeInput = useCallback(
    (name: string) => {
      const ref = `{{inputs.${name}}}`;
      const esc = escapeRegExp(name);
      const refRe = new RegExp(`\\$\\{inputs\\.${esc}\\}|\\{\\{inputs\\.${esc}\\}\\}`, "g");
      setConduit((prev) => {
        const { [name]: _, ...rest } = prev.inputs;
        return {
          ...prev,
          inputs: rest,
          tasks: prev.tasks.map((t) => ({
            ...t,
            task: t.task.replaceAll(ref, ""),
            ...(t.inputs
              ? {
                  inputs: Object.fromEntries(
                    Object.entries(t.inputs).map(([k, v]) => [k, v.replaceAll(refRe, "")]),
                  ),
                }
              : {}),
          })),
        };
      });
    },
    [setConduit],
  );

  const selectedTask = conduit.tasks.find((t) => t.name === selectedName);

  const handleSelect = useCallback(
    (task: ConduitTask | undefined) => setSelectedName(task?.name),
    [],
  );

  const handleSelectConduit = useCallback((name: string) => {
    const c = getConduitSync(name, allConduits);
    if (c) {
      setConduit(structuredClone(c));
      setSelectedName(undefined);
    }
  }, [setConduit, allConduits]);

  const handlePickerSelect = useCallback((name: string) => {
    handleSelectConduit(name);
    setPickerOpen(false);
  }, [handleSelectConduit]);

  const handleShowTools = useCallback(() => {
    setSelectedName(undefined);
  }, []);

  const handleCreate = () => {
    const name = createName.trim();
    if (!name) return;
    const timeout = Number(createTimeout) || undefined;
    const maxConcurrency = Number(createMaxConcurrency) || undefined;
    const draft: Conduit = {
      name,
      description: createDesc.trim(),
      timeout,
      maxConcurrency,
      runPath: "",
      inputs: {},
      tasks: [],
    };
    setConduit(draft);
    setCreateOpen(false);
    setCreateName("");
    setCreateDesc("");
    setCreateTimeout("3600");
    setCreateMaxConcurrency("1");
  };

  const canSave =
    conduit.name.trim() !== "" &&
    conduit.description.trim() !== "" &&
    conduit.tasks.length > 0 &&
    !interactionError(conduit.interaction);

  const handleSave = async () => {
    const policyError = interactionError(conduit.interaction);
    if (policyError) {
      toast.error(policyError);
      return;
    }
    setSaving(true);
    const payload = {
      name: conduit.name,
      description: conduit.description,
      inputs: conduit.inputs,
      timeout: conduit.timeout,
      maxConcurrency: conduit.maxConcurrency,
      interaction: conduit.interaction,
      // Fold the conditions map back into `depends_on` DSL strings — the
      // backend has no `conditional_on` field and would silently drop it,
      // turning every gate the designer drew into a plain dependency.
      tasks: toWireTasks(conduit.tasks),
    };
    const isExisting = allConduits.some((c) => c.name === conduit.name);
    try {
      if (isExisting) {
        await updateConduit(payload);
      } else {
        await createConduit(payload);
      }
      refresh();
      clearDraftConduit();
      toast.success(`Saved conduit ${conduit.name}`);
      // Navigate straight away rather than on a timer: the toast outlives the
      // route change, and the old setTimeout could fire after unmount.
      navigate("/dashboard");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to save conduit");
    } finally {
      setSaving(false);
    }
  };

  const handleInspect = useCallback(
    (task: ConduitTask) => {
      setSelectedName(task.name);
      if (isMobile) setInspectorOpen(true);
    },
    [isMobile],
  );

  return (
    <div
      data-route="designer"
      className="flex h-[calc(100dvh-3.5rem)] w-full border-b border-border"
    >
      <section className="relative flex-1">
        <Canvas
          conduit={conduit}
          onSelect={handleSelect}
          onInspect={handleInspect}
          onUpdateTask={updateTask}
          onDeleteTask={deleteTask}
          onShowTools={handleShowTools}
          positionRef={positionRef}
        />

        {/* Desktop: inline action buttons */}
        <div
          data-testid="designer-actions"
          className="pointer-events-none absolute inset-x-0 top-4 z-10 hidden justify-center lg:flex"
        >
          <div className="pointer-events-auto flex gap-2 rounded-sm border border-border bg-card/80 px-2 py-1 backdrop-blur">
            <Dialog open={pickerOpen} onOpenChange={setPickerOpen}>
              <DialogTrigger asChild>
                <Button variant="outline" size="sm" data-testid="open-conduit">
                  open conduit
                </Button>
              </DialogTrigger>
              <OpenConduitDialogContent
                allConduits={allConduits}
                currentName={conduit.name}
                onSelect={handlePickerSelect}
              />
            </Dialog>

            <div className="mx-1 h-5 w-px bg-border" />
            <Button variant="outline" size="sm" onClick={() => undo()} title="Undo (Ctrl+Z)">
              <Undo2 className="size-3.5" />
            </Button>
            <Button variant="outline" size="sm" onClick={() => redo()} title="Redo (Ctrl+Shift+Z / Ctrl+Y)">
              <Redo2 className="size-3.5" />
            </Button>
            <div className="mx-1 h-5 w-px bg-border" />

            <Dialog open={createOpen} onOpenChange={setCreateOpen}>
              <DialogTrigger asChild>
                <Button variant="outline" size="sm" data-testid="new-conduit">
                  new conduit
                </Button>
              </DialogTrigger>
              <CreateConduitDialogContent
                name={createName}
                setName={setCreateName}
                desc={createDesc}
                setDesc={setCreateDesc}
                timeout={createTimeout}
                setTimeout={setCreateTimeout}
                maxConcurrency={createMaxConcurrency}
                setMaxConcurrency={setCreateMaxConcurrency}
                onCreate={handleCreate}
              />
            </Dialog>

            <Sheet open={yamlOpen} onOpenChange={setYamlOpen}>
              <SheetTrigger asChild>
                <Button variant="outline" size="sm" data-testid="preview-yaml">
                  preview yaml
                </Button>
              </SheetTrigger>
              <PreviewYamlSheetContent conduit={conduit} />
            </Sheet>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={!canSave || saving}
              data-testid="designer-save"
            >
              <Play className="size-3.5" aria-hidden />
              save conduit
            </Button>
          </div>
        </div>

        {/* Mobile: menu at top-left. Radix Popover rather than a hand-rolled
            dropdown, which had no accessible name on the trigger, no
            aria-expanded, no Escape handling, no focus trap, and dismissed
            only via a pointer-only backdrop div. */}
        <div className="absolute left-3 top-3 z-dropdown lg:hidden">
          <Popover open={menuOpen} onOpenChange={setMenuOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                aria-label="Conduit actions"
                className="flex size-11 items-center justify-center rounded-sm border border-border bg-card/90 backdrop-blur hover:bg-muted"
              >
                <Menu className="size-4 text-foreground" />
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" className="flex w-[200px] flex-col gap-1 p-2">
              <Dialog open={pickerOpen} onOpenChange={(open) => { setPickerOpen(open); if (!open) setMenuOpen(false); }}>
                <DialogTrigger asChild>
                  <button type="button" className={MENU_ITEM}>
                    open conduit
                  </button>
                </DialogTrigger>
                <OpenConduitDialogContent
                  allConduits={allConduits}
                  currentName={conduit.name}
                  onSelect={(name) => { handlePickerSelect(name); setMenuOpen(false); }}
                />
              </Dialog>

              <Dialog open={createOpen} onOpenChange={(open) => { setCreateOpen(open); if (!open) setMenuOpen(false); }}>
                <DialogTrigger asChild>
                  <button type="button" className={MENU_ITEM}>
                    new conduit
                  </button>
                </DialogTrigger>
                <CreateConduitDialogContent
                  name={createName}
                  setName={setCreateName}
                  desc={createDesc}
                  setDesc={setCreateDesc}
                  timeout={createTimeout}
                  setTimeout={setCreateTimeout}
                  maxConcurrency={createMaxConcurrency}
                  setMaxConcurrency={setCreateMaxConcurrency}
                  onCreate={handleCreate}
                />
              </Dialog>

              <Sheet open={yamlOpen} onOpenChange={(open) => { setYamlOpen(open); if (!open) setMenuOpen(false); }}>
                <SheetTrigger asChild>
                  <button type="button" className={MENU_ITEM}>
                    preview yaml
                  </button>
                </SheetTrigger>
                <PreviewYamlSheetContent conduit={conduit} />
              </Sheet>

              <button
                type="button"
                onClick={() => { setMenuOpen(false); handleSave(); }}
                disabled={!canSave || saving}
                className={`${MENU_ITEM} gap-2 text-primary disabled:pointer-events-none disabled:opacity-40`}
              >
                <Play className="size-3.5" aria-hidden />
                save conduit
              </button>
            </PopoverContent>
          </Popover>
        </div>
      </section>

      {/* Desktop: inline sidebars */}
      {!isMobile && (
        selectedTask ? (
          <aside className="w-[304px] shrink-0 overflow-auto border-l border-border">
            <Inspector task={selectedTask} conduit={conduit} conduits={allConduits} onUpdateTask={updateTask} conduitInputs={conduit.inputs} onAddInput={addInput} />
          </aside>
        ) : (
          <aside className="w-[280px] shrink-0 overflow-auto border-l border-border">
            <ToolPanel conduit={conduit} conduitInputs={conduit.inputs} onAddTask={addTask} onAddInput={addInput} onRemoveInput={removeInput} onInteractionChange={(interaction) => setConduit((prev) => ({ ...prev, interaction }))} />
          </aside>
        )
      )}

      {/* Mobile: tool panel sheet — full screen from right */}
      <Sheet open={toolPanelOpen} onOpenChange={setToolPanelOpen}>
        <SheetContent side="right" className="w-full overflow-auto sm:max-w-full">
          <SheetHeader>
            <SheetTitle>tools</SheetTitle>
          </SheetHeader>
          <ToolPanel conduit={conduit} conduitInputs={conduit.inputs} onAddTask={(task) => { addTask(task); setToolPanelOpen(false); }} onAddInput={addInput} onRemoveInput={removeInput} onInteractionChange={(interaction) => setConduit((prev) => ({ ...prev, interaction }))} />
        </SheetContent>
      </Sheet>

      {/* Mobile: inspector sheet — full screen from right */}
      <Sheet open={inspectorOpen} onOpenChange={setInspectorOpen}>
        <SheetContent side="right" className="w-full overflow-auto sm:max-w-full">
          <SheetHeader>
            <SheetTitle>inspector</SheetTitle>
          </SheetHeader>
          {selectedTask && (
            <Inspector task={selectedTask} conduit={conduit} conduits={allConduits} onUpdateTask={updateTask} conduitInputs={conduit.inputs} onAddInput={addInput} />
          )}
        </SheetContent>
      </Sheet>

      {/* Mobile: floating tools tab */}
      {isMobile && (
        <button
          type="button"
          onClick={() => setToolPanelOpen(true)}
          className="fixed right-0 top-1/2 z-dropdown flex min-h-11 w-11 -translate-y-1/2 items-center justify-center rounded-l-md border border-r-0 border-border bg-card/90 py-3 font-mono text-mini uppercase tracking-[0.1em] text-foreground shadow-sm backdrop-blur hover:bg-muted"
        >
          tools
        </button>
      )}
    </div>
  );
}

// Shared dialog/sheet bodies, used by both the desktop toolbar and the mobile
// burger menu so the two never drift out of sync.

function OpenConduitDialogContent({
  allConduits,
  currentName,
  onSelect,
}: {
  allConduits: Conduit[];
  currentName: string;
  onSelect: (name: string) => void;
}) {
  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>
          Open a <em className="text-primary not-italic">conduit</em>
        </DialogTitle>
        <DialogDescription>
          Select an existing conduit to edit in the designer.
        </DialogDescription>
      </DialogHeader>
      <div className="max-h-[360px] overflow-auto">
        <div className="flex flex-col gap-1.5">
          {allConduits.map((c) => (
            <button
              key={c.name}
              type="button"
              onClick={() => onSelect(c.name)}
              className={`w-full rounded-md border px-3 py-2.5 text-left transition-colors ${
                c.name === currentName
                  ? "border-primary/50 bg-primary/8"
                  : "border-border/50 hover:border-border hover:bg-muted/40"
              }`}
            >
              <div
                className={`font-mono text-label leading-tight ${
                  c.name === currentName ? "text-primary" : "text-foreground"
                }`}
              >
                {c.name}
              </div>
              {c.description && (
                <div className="mt-1 truncate text-mini leading-snug text-muted-foreground">
                  {c.description}
                </div>
              )}
            </button>
          ))}
        </div>
      </div>
      <DialogFooter>
        <DialogClose asChild>
          <Button variant="outline" size="sm">cancel</Button>
        </DialogClose>
      </DialogFooter>
    </DialogContent>
  );
}

function CreateConduitDialogContent({
  name,
  setName,
  desc,
  setDesc,
  timeout,
  setTimeout,
  maxConcurrency,
  setMaxConcurrency,
  onCreate,
}: {
  name: string;
  setName: (v: string) => void;
  desc: string;
  setDesc: (v: string) => void;
  timeout: string;
  setTimeout: (v: string) => void;
  maxConcurrency: string;
  setMaxConcurrency: (v: string) => void;
  onCreate: () => void;
}) {
  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>
          Create a new <em className="text-primary not-italic">conduit</em>
        </DialogTitle>
        <DialogDescription>
          Name and describe the conduit. Add tasks in the canvas, then save when ready.
        </DialogDescription>
      </DialogHeader>
      <div className="grid gap-4 py-2">
        <div className="grid gap-2">
          <Label htmlFor="conduit-name">name</Label>
          <Input
            id="conduit-name"
            placeholder="e.g. my_pipeline"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCreate();
            }}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="conduit-desc">description</Label>
          <Input
            id="conduit-desc"
            placeholder="What this conduit does"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCreate();
            }}
          />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div className="grid gap-2">
            <Label htmlFor="conduit-timeout">timeout (seconds)</Label>
            <Input
              id="conduit-timeout"
              type="number"
              min={1}
              placeholder="3600"
              value={timeout}
              onChange={(e) => setTimeout(e.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="conduit-max-concurrency">max concurrency</Label>
            <Input
              id="conduit-max-concurrency"
              type="number"
              min={1}
              placeholder="1"
              value={maxConcurrency}
              onChange={(e) => setMaxConcurrency(e.target.value)}
            />
          </div>
        </div>
      </div>
      <DialogFooter>
        <DialogClose asChild>
          <Button variant="outline" size="sm">cancel</Button>
        </DialogClose>
        <Button size="sm" onClick={onCreate} data-testid="create-conduit-submit">
          create
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

function PreviewYamlSheetContent({ conduit }: { conduit: Conduit }) {
  return (
    <SheetContent
      side="right"
      className="flex w-full flex-col sm:max-w-[560px]"
      data-testid="yaml-sheet"
    >
      <SheetHeader>
        <SheetTitle>
          <em className="text-primary not-italic">preview</em> yaml
        </SheetTitle>
        <SheetDescription>
          read-only · rendered from current canvas state
        </SheetDescription>
      </SheetHeader>
      <ScrollArea className="flex-1 px-6 py-4">
        <pre
          data-testid="yaml-contents"
          className="whitespace-pre font-mono text-label leading-relaxed text-foreground"
        >
          {renderConduitYaml(conduit)}
        </pre>
      </ScrollArea>
    </SheetContent>
  );
}
