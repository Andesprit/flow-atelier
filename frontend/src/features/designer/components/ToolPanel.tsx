import { useState } from "react";
import { X } from "lucide-react";
import type { Conduit, ConduitTask, InputSpec, InteractionPolicy } from "@/types/conduit";
import { hintStr } from "@/types/conduit";
import { InteractionSettings } from "./InteractionSettings";
import { TOOL_META, type ToolMeta } from "@/constants/tools";

interface Props {
  conduit: Conduit;
  conduitInputs: Record<string, string | InputSpec>;
  onAddTask: (task: ConduitTask) => void;
  onAddInput: (name: string, hint: string) => void;
  onRemoveInput: (name: string) => void;
  onInteractionChange: (value: InteractionPolicy | null) => void;
}

export function ToolPanel({ conduit, conduitInputs, onAddTask, onAddInput, onRemoveInput, onInteractionChange }: Props) {
  const [isCreatingInput, setIsCreatingInput] = useState(false);
  const [newInputName, setNewInputName] = useState("");
  const [newInputHint, setNewInputHint] = useState("");

  const handleAdd = (tool: ToolMeta) => {
    const task: ConduitTask = {
      name: "",
      tool: tool.name,
      description: "",
      task: "",
      dependsOn: [],
    };
    onAddTask(task);
  };

  const handleCreateInput = () => {
    const name = newInputName.trim();
    if (!name) return;
    if (name in conduitInputs) return;
    onAddInput(name, newInputHint.trim());
    setNewInputName("");
    setNewInputHint("");
    setIsCreatingInput(false);
  };

  return (
    <div
      data-testid="designer-tool-panel"
      className="overflow-auto bg-background"
    >
      <div className="border-b border-border/60 px-5 py-5">
        <div className="font-mono text-micro uppercase tracking-[0.18em] text-muted-foreground">
          conduit
        </div>
        <h2 className="mt-1 font-display text-panel leading-tight tracking-[-0.01em] text-foreground">
          {conduit.name || "untitled"}
        </h2>
        {conduit.description && (
          <p className="mt-1 text-label leading-snug text-muted-foreground">
            {conduit.description}
          </p>
        )}
      </div>

      <InteractionSettings value={conduit.interaction} onChange={onInteractionChange} />

      <div className="px-4 py-4">
        <div className="flex flex-col gap-2">
          {TOOL_META.map((t) => (
            <button
              key={t.name}
              type="button"
              onClick={() => handleAdd(t)}
              className="group w-full rounded-md border border-border/60 bg-background px-3 py-2.5 text-left transition-colors hover:border-border hover:bg-muted/50"
            >
              <div
                className="font-mono text-body leading-tight font-bold"
                style={{ color: t.color }}
              >
                {t.name}
              </div>
              <div className="mt-1 text-label leading-snug text-muted-foreground">
                {t.desc}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="border-t border-border/60 px-4 py-5">
        <span className="mb-3 block font-mono text-micro uppercase tracking-[0.18em] text-muted-foreground">
          inputs
        </span>
        {Object.keys(conduitInputs).length > 0 && (
          <div className="mb-3 flex flex-col gap-1">
            {Object.entries(conduitInputs).map(([name, hint]) => (
              <div
                key={name}
                className="flex items-start justify-between gap-1 rounded-md border border-border/50 px-2.5 py-2"
              >
                <div className="min-w-0">
                  <div className="font-mono text-label leading-tight text-foreground">
                    {name}
                  </div>
                  {hintStr(hint) && (
                    <div className="mt-0.5 truncate text-mini text-muted-foreground">
                      {hintStr(hint)}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  aria-label={`remove input ${name}`}
                  onClick={() => onRemoveInput(name)}
                  className="-mr-1 flex size-6 shrink-0 items-center justify-center text-muted-foreground hover:text-destructive"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </div>
            ))}
          </div>
        )}
        {isCreatingInput ? (
          <div className="space-y-2 border border-border/60 p-2">
            <label htmlFor="toolpanel-input-name" className="sr-only">input name</label>
            <input
              id="toolpanel-input-name"
              placeholder="name"
              value={newInputName}
              onChange={(e) => setNewInputName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreateInput();
              }}
              className="h-11 w-full border-0 border-b border-border-strong bg-transparent font-mono text-label text-foreground focus:border-primary"
            />
            <label htmlFor="toolpanel-input-desc" className="sr-only">input description</label>
            <input
              id="toolpanel-input-desc"
              placeholder="description"
              value={newInputHint}
              onChange={(e) => setNewInputHint(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreateInput();
              }}
              className="h-11 w-full border-0 border-b border-border-strong bg-transparent font-mono text-label text-foreground focus:border-primary"
            />
            <button
              type="button"
              onClick={handleCreateInput}
              className="h-11 w-full border border-primary font-mono text-mini uppercase tracking-[0.14em] text-primary hover:bg-primary hover:text-primary-foreground"
            >
              create
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setIsCreatingInput(true)}
            className="flex h-11 items-center gap-1 font-mono text-label text-primary hover:underline"
          >
            + add input
          </button>
        )}
      </div>
    </div>
  );
}
