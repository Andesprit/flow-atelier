import { type FormEvent, useMemo, useState, useEffect, useCallback } from "react";
import type { Conduit } from "@/types/conduit";
import { hintStr, inputDefault, inputHint, submittedInputs } from "@/types/conduit";
import type { ScheduleConfig } from "@/types/schedule";
import { Button } from "@/components/ui/button";
import { Play, Clock } from "lucide-react";
import { saveConduitInputs, loadConduitInputs } from "@/services/api/conduit-inputs";
import { loadRunPath, saveRunPath } from "@/services/api/conduit-run-path";
import { ScheduleDialog } from "./schedule-dialog";

interface Props {
  conduit: Conduit;
  onRun: (inputs: Record<string, string>, runPath: string) => void;
  onSchedule?: (inputs: Record<string, string>, config: ScheduleConfig, scheduleConduitName?: string, runPath?: string) => void;
}

export function InputForm({ conduit, onRun, onSchedule }: Props) {
  const defaults = useMemo(
    () =>
      Object.fromEntries(
        Object.keys(conduit.inputs).map((name) => [name, ""]),
      ),
    [conduit],
  );

  const [values, setValues] = useState<Record<string, string>>(() => {
    const saved = loadConduitInputs(conduit.name);
    if (saved) {
      const merged = { ...defaults };
      for (const key of Object.keys(defaults)) {
        if (key in saved) merged[key] = saved[key];
      }
      return merged;
    }
    return defaults;
  });
  const [schedulerOpen, setSchedulerOpen] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const [runPath, setRunPath] = useState(() =>
    loadRunPath(conduit.name, conduit.runPath ?? ""),
  );

  useEffect(() => {
    const saved = loadConduitInputs(conduit.name);
    if (saved) {
      const merged = { ...defaults };
      for (const key of Object.keys(defaults)) {
        if (key in saved) merged[key] = saved[key];
      }
      setValues(merged);
    } else {
      setValues(defaults);
    }
  }, [conduit.name, defaults]);

  useEffect(() => setRunPath(loadRunPath(conduit.name, conduit.runPath ?? "")), [conduit.name, conduit.runPath]);

  const handleChange = useCallback(
    (name: string, value: string) => {
      setValues((prev) => ({ ...prev, [name]: value }));
    },
    [],
  );

  const handleBlur = useCallback(() => {
    setValues((current) => {
      saveConduitInputs(conduit.name, current);
      return current;
    });
  }, [conduit.name]);

  const handlePathBlur = useCallback(
    (value: string) => {
      setRunPath(value);
      saveRunPath(conduit.name, value);
    },
    [conduit.name],
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!runPath.trim()) errs.runPath = "Working directory is required";
    for (const [name, hint] of Object.entries(conduit.inputs)) {
      if (!values[name]?.trim() && inputDefault(hint) == null) errs[name] = "Required";
    }
    if (Object.keys(errs).length) { setErrors(errs); return; }
    setErrors({});
    onRun(submittedInputs(conduit, values), runPath);
  };

  return (
    <form onSubmit={submit} data-testid="input-form" className="mt-10">
      <h2 className="mb-3 font-display text-panel text-foreground">Run path</h2>

      <div className="border-t border-border/60">
        {/* The 140px label column was unconditional: at 390px it left 146px of
            width for a filesystem path. Labels stack above the field below sm. */}
        <div className="grid grid-cols-1 items-start gap-1 border-b border-border/50 py-4 sm:grid-cols-[140px_1fr] sm:gap-6">
          <label htmlFor="run-path" className="flex items-center font-mono text-label uppercase tracking-[0.12em] text-foreground sm:h-11">
            working directory
          </label>
          <div>
            <input
              id="run-path"
              value={runPath}
              onChange={(e) => setRunPath(e.target.value)}
              onBlur={(e) => handlePathBlur(e.target.value)}
              aria-invalid={!!errors.runPath}
              aria-describedby={errors.runPath ? "run-path-error" : undefined}
              className="h-11 w-full border-0 border-b border-border-strong bg-transparent font-mono text-data text-foreground focus:border-primary"
              placeholder="/path/to/project"
            />
            {errors.runPath && <div id="run-path-error" className="mt-1 font-mono text-mini text-destructive">{errors.runPath}</div>}
          </div>
        </div>
      </div>

      {Object.keys(conduit.inputs).length > 0 && (
      <>
        <h2 className="mt-8 mb-3 font-display text-panel text-foreground">
          Inputs for <span className="font-mono text-data">{conduit.name}</span>
        </h2>

        <div className="border-t border-border/60">
          {Object.entries(conduit.inputs).map(([name, hint]) => (
          <div
            key={name}
            className="grid grid-cols-1 items-start gap-1 border-b border-border/50 py-4 sm:grid-cols-[140px_1fr] sm:gap-6"
          >
            <div>
              <label htmlFor={`input-${name}`} className="font-mono text-label uppercase tracking-[0.12em] text-foreground">
                {name}
              </label>
              <div className="mt-1 text-label text-muted-foreground">
                {inputHint(hint)}
              </div>
            </div>
            <div>
              <input
                id={`input-${name}`}
                name={name}
                value={values[name] ?? ""}
                onChange={(e) => handleChange(name, e.target.value)}
                onBlur={handleBlur}
                aria-invalid={!!errors[name]}
                aria-describedby={errors[name] ? `input-${name}-error` : undefined}
                className="h-11 w-full border-0 border-b border-border-strong bg-transparent font-mono text-data text-foreground focus:border-primary"
                placeholder={inputDefault(hint) ?? hintStr(hint)}
              />
              {errors[name] && <div id={`input-${name}-error`} className="mt-1 font-mono text-mini text-destructive">{errors[name]}</div>}
            </div>
          </div>
        ))}
      </div>
      </>
      )}
      {/* Glyph icons (◷ ▸) used to sit inside the accessible name, so the
          primary action announced as "black right-pointing small triangle, run
          conduit". lucide is already the app's icon set; these are aria-hidden.
          Default size, not sm: this is the product's primary action. */}
      <div className="flex flex-wrap justify-end gap-2 pt-8">
        {onSchedule && (
          <Button
            type="button"
            variant="outline"
            data-testid="schedule-conduit"
            onClick={() => setSchedulerOpen(true)}
          >
            <Clock className="size-3.5" aria-hidden />
            schedule
          </Button>
        )}
        <Button type="submit" data-testid="run-conduit">
          <Play className="size-3.5" aria-hidden />
          run conduit
        </Button>
      </div>

      {onSchedule && (
        <ScheduleDialog
          conduitName={conduit.name}
          defaultInputs={values}
          defaultRunPath={runPath}
          open={schedulerOpen}
          onOpenChange={setSchedulerOpen}
          onConfirm={(config, conduitName, inputs, runPath) => onSchedule(inputs, config, conduitName, runPath)}
        />
      )}
    </form>
  );
}
