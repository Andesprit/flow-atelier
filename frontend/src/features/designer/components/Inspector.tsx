import { useState, useEffect, useId, useRef } from "react";
import { X } from "lucide-react";
import type { Conduit, ConduitTask, InputSpec } from "@/types/conduit";
import { hintStr, slugifyTaskName } from "@/types/conduit";
import { Badge } from "@/components/ui/badge";
import { withoutCondition } from "@/utils/conditions";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { TOOL_META, toolColor } from "@/constants/tools";

interface Props {
  task: ConduitTask | undefined;
  conduit: Conduit;
  conduits: Conduit[];
  onUpdateTask: (name: string, partial: Partial<ConduitTask>) => void;
  conduitInputs: Record<string, string | InputSpec>;
  onAddInput: (name: string, hint: string) => void;
}

export function Inspector({ task, conduit, conduits, onUpdateTask, conduitInputs, onAddInput }: Props) {
  const [draft, setDraft] = useState<ConduitTask | undefined>(task);

  const [repeatOpen, setRepeatOpen] = useState(false);
  const [repeatCustom, setRepeatCustom] = useState(false);
  const [conduitPickerOpen, setConduitPickerOpen] = useState(false);

  const [inputsOpen, setInputsOpen] = useState(false);
  const [isCreatingInput, setIsCreatingInput] = useState(false);
  const [newInputName, setNewInputName] = useState("");
  const [newInputHint, setNewInputHint] = useState("");
  const taskRef = useRef<HTMLTextAreaElement>(null);
  const agentListId = useId();

  useEffect(() => setDraft(task), [task]);

  if (!task || !draft) {
    return (
      <div
        data-testid="designer-inspector"
        className="overflow-auto bg-background p-7"
      >
        <header className="mb-4 border-b border-border/60 pb-4">
          <div className="font-mono text-micro uppercase tracking-[0.16em] text-muted-foreground">
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
        </header>
        <div className="font-mono text-label text-muted-foreground">
          Select a task on the canvas to inspect it.
        </div>
      </div>
    );
  }

  const commit = (patch: Partial<ConduitTask>) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    onUpdateTask(task.name, patch);
  };

  const taskInputs = draft.inputs ?? {};
  const setTaskInput = (key: string, value: string) => {
    commit({ inputs: { ...taskInputs, [key]: value } });
  };
  const removeTaskInput = (key: string) => {
    const { [key]: _, ...rest } = taskInputs;
    commit({ inputs: Object.keys(rest).length === 0 ? undefined : rest });
  };

  const selectedConduit = draft.tool === "tool:conduit" && draft.task
    ? conduits.find((c) => c.name === draft.task)
    : undefined;
  const harnessParts = draft.tool.startsWith("harness:") ? draft.tool.split(":") : null;
  const loopMode = draft.until != null ? "until" : draft.while != null ? "while" : "none";
  const loopPredicate = draft.until ?? draft.while ?? "";

  const setHarness = (agent: string, model: string, effort: string) => {
    const tool = `harness:${agent}${model ? `:${model}${effort ? `:${effort}` : ""}` : ""}` as ConduitTask["tool"];
    commit({ tool });
  };

  const [hitlNewKey, setHitlNewKey] = useState("");
  const [hitlNewVal, setHitlNewVal] = useState("");
  const [hitlAdding, setHitlAdding] = useState(false);

  const handleHitlAdd = () => {
    const k = hitlNewKey.trim();
    if (!k) return;
    commit({ inputs: { ...taskInputs, [k]: hitlNewVal.trim() } });
    setHitlNewKey("");
    setHitlNewVal("");
    setHitlAdding(false);
  };

  const handleInsertInput = (inputName: string) => {
    const ref = `{{inputs.${inputName}}}`;
    const textarea = taskRef.current;
    if (textarea) {
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const text = draft.task;
      commit({ task: text.substring(0, start) + ref + text.substring(end) });
      setTimeout(() => {
        textarea.selectionStart = textarea.selectionEnd = start + ref.length;
        textarea.focus();
      }, 0);
    } else {
      commit({ task: draft.task + ref });
    }
    setInputsOpen(false);
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
      data-testid="designer-inspector"
      className="overflow-auto bg-background p-7"
    >
      <header className="mb-5 border-b border-border/60 pb-4">
        <div className="font-mono text-micro uppercase tracking-[0.16em] text-muted-foreground">
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
      </header>

      <header className="mb-5 border-b border-border/60 pb-4">
        <div className="font-mono text-micro uppercase tracking-[0.16em] text-muted-foreground">
          task
        </div>
        <div
          className="mt-1 font-mono text-data font-bold leading-tight"
          style={{ color: toolColor(task.tool) }}
        >
          {task.tool}
        </div>
        {Object.entries(task.conditions ?? {}).map(([source, c]) => (
          <Badge key={source} variant="primary" className="mt-2">
            {source} · {c.kind} · {c.pattern}
          </Badge>
        ))}
      </header>

      <section className="space-y-5">
        {harnessParts && (
          <div className="space-y-3 border-b border-border/60 pb-5">
            <Field label="agent">
              <input
                aria-label="agent"
                list={agentListId}
                value={harnessParts[1] ?? ""}
                onChange={(e) => setHarness(e.target.value, harnessParts[2] ?? "", harnessParts[3] ?? "")}
                placeholder="choose or enter another harness"
                className="w-full border-0 border-b border-border-strong bg-transparent pb-1.5 font-mono text-body text-foreground focus:border-primary"
              />
              <datalist id={agentListId}>
                {TOOL_META.filter((item) => item.name.startsWith("harness:")).map((item) => (
                  <option key={item.name} value={item.name.slice("harness:".length)} />
                ))}
              </datalist>
            </Field>
            <Field label="model (optional)">
              <input
                aria-label="model"
                value={harnessParts[2] ?? ""}
                onChange={(e) => setHarness(harnessParts[1] ?? "", e.target.value, harnessParts[3] ?? "")}
                placeholder="model id from your agent"
                className="w-full border-0 border-b border-border-strong bg-transparent pb-1.5 font-mono text-body text-foreground focus:border-primary"
              />
            </Field>
            <Field label="effort (optional)">
              <input
                aria-label="effort"
                value={harnessParts[3] ?? ""}
                disabled={!harnessParts[2]}
                onChange={(e) => setHarness(harnessParts[1] ?? "", harnessParts[2] ?? "", e.target.value)}
                placeholder="requires a model"
                className="w-full border-0 border-b border-border-strong bg-transparent pb-1.5 font-mono text-body text-foreground focus:border-primary disabled:opacity-50"
              />
            </Field>
            <p className="font-sans text-mini text-muted-foreground">Choose a palette agent or type another installed harness. Model and effort must be offered by that agent.</p>
          </div>
        )}
        <Field label="name">
          <input
            value={draft.name}
            onChange={(e) => {
              const newName = slugifyTaskName(e.target.value);
              setDraft((d) => d ? { ...d, name: newName } : d);
              if (newName) onUpdateTask(task.name, { name: newName });
            }}
            className="w-full border-0 border-b border-border-strong bg-transparent pb-1.5 font-mono text-body text-foreground focus:border-primary"
          />
        </Field>
        <Field label="description">
          <input
            value={draft.description}
            onChange={(e) => commit({ description: e.target.value })}
            className="w-full border-0 border-b border-border-strong bg-transparent pb-1.5 font-mono text-body text-foreground focus:border-primary"
          />
        </Field>
        {draft.tool !== "tool:conduit" && (
        <Field label="inputs">
          <Popover open={inputsOpen} onOpenChange={setInputsOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                className="flex w-full items-center justify-between border border-border/60 bg-transparent px-2 py-1.5 text-left font-mono text-label text-foreground hover:border-primary focus-visible:outline-2 focus-visible:outline-primary"
              >
                <span>
                  {Object.keys(conduitInputs).length === 0
                    ? "create new input"
                    : "select input…"}
                </span>
                <span className="text-muted-foreground">▾</span>
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" className="w-[var(--radix-popover-trigger-width)] p-0">
              {Object.keys(conduitInputs).map((name) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => handleInsertInput(name)}
                  className="w-full px-2 py-1.5 text-left font-mono text-label text-foreground hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                >
                  {name}
                </button>
              ))}
              <button
                type="button"
                onClick={() => {
                  setInputsOpen(false);
                  setIsCreatingInput(true);
                }}
                className="w-full border-t border-border/60 px-2 py-1.5 text-left font-mono text-label text-primary hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
              >
                + create new input
              </button>
            </PopoverContent>
          </Popover>
          {isCreatingInput && (
            <div className="mt-2 space-y-2 border border-border/60 p-2">
              <input
                placeholder="name"
                value={newInputName}
                onChange={(e) => setNewInputName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleCreateInput();
                }}
                className="w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
              />
              <input
                placeholder="description"
                value={newInputHint}
                onChange={(e) => setNewInputHint(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleCreateInput();
                }}
                className="w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
              />
              <button
                type="button"
                onClick={handleCreateInput}
                className="w-full border border-primary py-1 font-mono text-mini uppercase tracking-[0.14em] text-primary hover:bg-primary hover:text-primary-foreground"
              >
                create
              </button>
            </div>
          )}
        </Field>
        )}
        <Field label={draft.tool === "tool:conduit" ? "conduit" : "task"}>
          {draft.tool === "tool:conduit" && draft.tasks ? (
            <div className="border border-border/60 px-2 py-1.5 font-mono text-label text-foreground">
              Inline body · {draft.tasks.length} tasks
            </div>
          ) : draft.tool === "tool:conduit" ? (
            <Popover open={conduitPickerOpen} onOpenChange={setConduitPickerOpen}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="flex w-full items-center justify-between border border-border/60 bg-transparent px-2 py-1.5 text-left font-mono text-label text-foreground hover:border-primary focus-visible:outline-2 focus-visible:outline-primary"
                >
                  <span className={draft.task ? "text-foreground" : "text-muted-foreground"}>
                    {draft.task || "select conduit…"}
                  </span>
                  <span className="text-muted-foreground">▾</span>
                </button>
              </PopoverTrigger>
              <PopoverContent
                align="start"
                className="max-h-[240px] w-[var(--radix-popover-trigger-width)] overflow-auto p-0"
              >
                {conduits.map((c) => (
                  <button
                    key={c.name}
                    type="button"
                    onClick={() => {
                      commit({ task: c.name });
                      setConduitPickerOpen(false);
                    }}
                    className={`w-full px-2 py-1.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none ${
                      draft.task === c.name ? "bg-primary/8" : ""
                    }`}
                  >
                    <div className={`font-mono text-label leading-tight ${
                      draft.task === c.name ? "text-primary" : "text-foreground"
                    }`}>
                      {c.name}
                    </div>
                    {c.description && (
                      <div className="mt-0.5 truncate text-mini leading-snug text-muted-foreground">
                        {c.description}
                      </div>
                    )}
                  </button>
                ))}
              </PopoverContent>
            </Popover>
          ) : (
            <textarea
              ref={taskRef}
              value={draft.task}
              onChange={(e) => commit({ task: e.target.value })}
              rows={8}
              className="w-full resize-y border border-border/60 bg-transparent p-2 font-mono text-label text-foreground focus:border-primary"
            />
          )}
        </Field>
        {(draft.tool === "tool:conduit" || draft.tool === "tool:hitl") && (
          <Field label="task inputs">
            {draft.tool === "tool:conduit" && draft.tasks ? (
              <div className="space-y-2">
                {Object.entries(taskInputs).map(([key, value]) => (
                  <label key={key} className="block font-mono text-mini text-muted-foreground">
                    {key}
                    <input
                      value={value}
                      onChange={(e) => setTaskInput(key, e.target.value)}
                      className="mt-1 w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
                    />
                  </label>
                ))}
              </div>
            ) : draft.tool === "tool:conduit" && selectedConduit ? (
              <div className="space-y-2">
                {Object.entries(selectedConduit.inputs).map(([key, hint]) => (
                  <div key={key} className="space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-mini text-muted-foreground">
                        {key}
                      </span>
                      {taskInputs[key] !== undefined && (
                        <button
                          type="button"
                          aria-label={`Remove input ${key}`}
                          className="flex size-6 shrink-0 items-center justify-center text-muted-foreground hover:text-destructive"
                          onClick={() => removeTaskInput(key)}
                        >
                          <X className="size-3.5" aria-hidden />
                        </button>
                      )}
                    </div>
                    <input
                      value={taskInputs[key] ?? ""}
                      onChange={(e) => setTaskInput(key, e.target.value)}
                      placeholder={hintStr(hint)}
                      className="w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
                    />
                  </div>
                ))}
                {Object.keys(selectedConduit.inputs).length === 0 && (
                  <span className="font-mono text-label text-muted-foreground">
                    — no inputs on selected conduit —
                  </span>
                )}
              </div>
            ) : draft.tool === "tool:hitl" ? (
              <div className="space-y-2">
                {Object.entries(taskInputs).map(([key, val]) => (
                  <div key={key} className="flex items-start gap-1.5">
                    <div className="min-w-0 flex-1">
                      <div className="font-mono text-mini text-muted-foreground">{key}</div>
                      <input
                        value={val}
                        onChange={(e) => setTaskInput(key, e.target.value)}
                        className="w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
                      />
                    </div>
                    <button
                      type="button"
                      aria-label={`Remove input ${key}`}
                      className="mt-3 flex size-6 shrink-0 items-center justify-center text-muted-foreground hover:text-destructive"
                      onClick={() => removeTaskInput(key)}
                    >
                      <X className="size-3.5" aria-hidden />
                    </button>
                  </div>
                ))}
                {hitlAdding ? (
                  <div className="space-y-1.5 border border-border/60 p-2">
                    <input
                      autoFocus
                      placeholder="input name"
                      value={hitlNewKey}
                      onChange={(e) => setHitlNewKey(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") handleHitlAdd(); }}
                      className="w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
                    />
                    <input
                      placeholder="prompt text"
                      value={hitlNewVal}
                      onChange={(e) => setHitlNewVal(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") handleHitlAdd(); }}
                      className="w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
                    />
                    <button
                      type="button"
                      onClick={handleHitlAdd}
                      className="w-full border border-primary py-1 font-mono text-mini uppercase tracking-[0.14em] text-primary hover:bg-primary hover:text-primary-foreground"
                    >
                      add
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setHitlAdding(true)}
                    className="flex items-center gap-1 font-mono text-label text-primary hover:underline"
                  >
                    + add input
                  </button>
                )}
              </div>
            ) : draft.tool === "tool:conduit" ? (
              <span className="font-mono text-label text-muted-foreground">
                — select a conduit first —
              </span>
            ) : null}
          </Field>
        )}
        <Field label="repeat">
          <div>
            <Popover
              open={repeatOpen}
              onOpenChange={(o) => {
                setRepeatOpen(o);
                if (o) setRepeatCustom(false);
              }}
            >
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="flex w-full items-center justify-between border border-border/60 bg-transparent px-2 py-1.5 text-left font-mono text-label text-foreground hover:border-primary focus-visible:outline-2 focus-visible:outline-primary"
                >
                  <span>{draft.repeat ? `×${draft.repeat}` : "off"}</span>
                  <span className="text-muted-foreground">▾</span>
                </button>
              </PopoverTrigger>
              <PopoverContent align="start" className="w-[var(--radix-popover-trigger-width)] bg-muted p-0">
                {[undefined, 2, 3, 4, 5].map((n) => (
                  <button
                    key={n ?? "off"}
                    type="button"
                    onClick={() => {
                      commit(n == null ? { repeat: n, until: null, while: null, onExhaust: "complete" } : { repeat: n });
                      setRepeatOpen(false);
                      setRepeatCustom(false);
                    }}
                    className={`w-full px-2 py-1.5 text-left font-mono text-label hover:bg-muted focus-visible:bg-muted focus-visible:outline-none ${
                      draft.repeat === n ? "text-primary" : "text-foreground"
                    }`}
                  >
                    {n ? `×${n}` : "off"}
                  </button>
                ))}
                <button
                  type="button"
                  onClick={() => {
                    setRepeatCustom(true);
                    setRepeatOpen(false);
                  }}
                  className="w-full border-t border-border/60 px-2 py-1.5 text-left font-mono text-label text-primary hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                >
                  custom…
                </button>
              </PopoverContent>
            </Popover>
            {repeatCustom && (
              <input
                autoFocus
                type="number"
                min={2}
                placeholder="enter count…"
                defaultValue={draft.repeat ?? ""}
                onBlur={(e) => {
                  const v = Number((e.target as HTMLInputElement).value);
                  if (v >= 2) commit({ repeat: v });
                  setRepeatCustom(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    const v = Number((e.target as HTMLInputElement).value);
                    if (v >= 2) {
                      commit({ repeat: v });
                      setRepeatCustom(false);
                    }
                  }
                }}
                className="mt-2 w-full border border-border/60 bg-muted px-2 py-1.5 font-mono text-label text-foreground focus:border-primary"
              />
            )}
          </div>
        </Field>
        {(draft.repeat ?? 1) >= 2 && (
          <div className="space-y-3 border-b border-border/60 pb-5">
            <Field label="stop when">
              <select
                aria-label="stop when"
                value={loopMode}
                onChange={(e) => {
                  const mode = e.target.value;
                  commit({
                    until: mode === "until" ? loopPredicate || "output.match()" : null,
                    while: mode === "while" ? loopPredicate || "output.match()" : null,
                    ...(mode === "none" ? { onExhaust: "complete" as const } : {}),
                  });
                }}
                className="w-full border border-border/60 bg-background px-2 py-1.5 font-mono text-label text-foreground focus:border-primary"
              >
                <option value="none">none — run every iteration</option>
                <option value="until">until — stop when true</option>
                <option value="while">while — continue while true</option>
              </select>
            </Field>
            {loopMode !== "none" && (
              <Field label="output condition">
                <input
                  aria-label="output condition"
                  value={loopPredicate}
                  onChange={(e) => commit({ [loopMode]: e.target.value })}
                  placeholder="output.match(TESTS PASSED)"
                  className="w-full border-0 border-b border-border-strong bg-transparent pb-1.5 font-mono text-label text-foreground focus:border-primary"
                />
                <p className="mt-1 font-sans text-mini text-muted-foreground">Use output.match(regex) or output.not_match(regex).</p>
              </Field>
            )}
            <label className="flex items-start gap-2 font-sans text-label text-foreground">
              <input
                type="checkbox"
                checked={loopMode !== "none" && draft.onExhaust === "fail"}
                disabled={loopMode === "none"}
                onChange={(e) => commit({ onExhaust: e.target.checked ? "fail" : "complete" })}
                className="mt-0.5"
              />
              <span>Fail the run if the condition is never met</span>
            </label>
            {loopMode === "none" && <p className="font-sans text-mini text-muted-foreground">Set a stop condition to enable this option.</p>}
            {loopMode !== "none" && draft.onExhaust !== "fail" && <p className="font-sans text-mini text-muted-foreground">If the limit is reached, dependent tasks run on the last result.</p>}
          </div>
        )}
        {draft.tool === "tool:conduit" && draft.tasks && (
          <Field label="inline loop body">
            <ol className="space-y-1 border border-border/60 p-2">
              {draft.tasks.map((child) => (
                <li key={child.name} className="font-mono text-label text-foreground">
                  {child.name} <span className="text-muted-foreground">· {child.tool}</span>
                </li>
              ))}
            </ol>
            <p className="mt-2 font-sans text-mini text-muted-foreground">Edit body tasks in conduit.yaml, or swap an agent for one run with --agent {draft.name}.TASK=HARNESS.</p>
          </Field>
        )}
        <Field label="depends on">
          <div>
            {draft.dependsOn.length === 0 ? (
              <span className="font-mono text-label text-muted-foreground">
                — none —
              </span>
            ) : (
              draft.dependsOn.map((dep) => {
                const condition = draft.conditions?.[dep];
                const edgeKind = condition ? condition.kind : "depends_on";
                const pattern = condition ? condition.pattern : "";
                return (
                  <div
                    key={dep}
                    className="border-b border-dashed border-border/50 py-2"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-label text-foreground/80">{dep}</span>
                      <button
                        type="button"
                        aria-label={`Remove dependency ${dep}`}
                        className="flex size-6 shrink-0 items-center justify-center text-muted-foreground hover:text-destructive"
                        onClick={() => {
                          const patch: Partial<ConduitTask> = {
                            dependsOn: draft.dependsOn.filter((x) => x !== dep),
                            conditions: withoutCondition(draft, dep),
                          };
                          commit(patch);
                        }}
                      >
                        <X className="size-3.5" aria-hidden />
                      </button>
                    </div>
                    <div className="mt-1.5 flex items-center gap-1.5">
                      {(["depends_on", "match", "not_match"] as const).map((kind) => (
                        <button
                          key={kind}
                          type="button"
                          onClick={() => {
                            if (kind === "depends_on") {
                              commit({ conditions: withoutCondition(draft, dep) });
                            } else {
                              commit({
                                conditions: { ...draft.conditions, [dep]: { kind, pattern } },
                              });
                            }
                          }}
                          className={`rounded px-1.5 py-0.5 font-mono text-micro uppercase tracking-[0.1em] transition-colors ${
                            edgeKind === kind
                              ? "bg-primary/15 text-primary"
                              : "text-muted-foreground hover:text-foreground"
                          }`}
                        >
                          {kind === "not_match" ? "not match" : kind.replace("_", " ")}
                        </button>
                      ))}
                    </div>
                    {edgeKind !== "depends_on" && (
                      <input
                        value={pattern}
                        onChange={(e) =>
                          commit({
                            conditions: {
                              ...draft.conditions,
                              [dep]: { kind: edgeKind as "match" | "not_match", pattern: e.target.value },
                            },
                          })
                        }
                        placeholder="regex pattern…"
                        className="mt-1.5 w-full border-0 border-b border-border-strong bg-transparent pb-1 font-mono text-label text-foreground focus:border-primary"
                      />
                    )}
                  </div>
                );
              })
            )}
          </div>
        </Field>
      </section>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <span className="mb-2 block font-mono text-micro uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </span>
      {children}
    </div>
  );
}
