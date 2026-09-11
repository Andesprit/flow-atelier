import { useState, useEffect } from "react";
import { Clock, X } from "lucide-react";
import type { ScheduleConfig } from "@/types/schedule";
import { hintStr, inputDefault, inputHint, submittedInputs } from "@/types/conduit";
import { useConduits, getConduitSync } from "@/services/ConduitProvider";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { cn } from "@/lib/cn";

interface Props {
  conduitName: string;
  defaultInputs?: Record<string, string>;
  defaultRunPath?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (
    config: ScheduleConfig,
    conduitName: string,
    inputs: Record<string, string>,
    runPath: string,
  ) => void;
}

const DAYS = [
  { value: 1, label: "mon" },
  { value: 2, label: "tue" },
  { value: 3, label: "wed" },
  { value: 4, label: "thu" },
  { value: 5, label: "fri" },
  { value: 6, label: "sat" },
  { value: 7, label: "sun" },
] as const;

function localIsoDefault() {
  const d = new Date();
  d.setMinutes(d.getMinutes() + 60);
  d.setSeconds(0, 0);
  return d.toISOString().slice(0, 16);
}

function toLocalIso(datetimeLocal: string) {
  const d = new Date(datetimeLocal);
  const offsetMs = d.getTimezoneOffset() * 60000;
  const local = new Date(d.getTime() - offsetMs);
  const offsetMin = d.getTimezoneOffset();
  const sign = offsetMin <= 0 ? "+" : "-";
  const absOffset = Math.abs(offsetMin);
  const hh = String(Math.floor(absOffset / 60)).padStart(2, "0");
  const mm = String(absOffset % 60).padStart(2, "0");
  return local.toISOString().replace("Z", "") + sign + hh + ":" + mm;
}

export function ScheduleDialog({
  conduitName,
  defaultInputs,
  defaultRunPath,
  open,
  onOpenChange,
  onConfirm,
}: Props) {
  const { conduits } = useConduits();
  const [mode, setMode] = useState<"once" | "recurring">("once");
  const [runAt, setRunAt] = useState(localIsoDefault);
  const [selectedDays, setSelectedDays] = useState<number[]>([]);
  const [times, setTimes] = useState<string[]>(["09:00"]);
  const [name, setName] = useState("");

  const [selectedConduitName, setSelectedConduitName] = useState(conduitName);
  const [conduitPickerOpen, setConduitPickerOpen] = useState(false);
  const [inputValues, setInputValues] = useState<Record<string, string>>(
    defaultInputs ?? {},
  );
  const [runPath, setRunPath] = useState(defaultRunPath ?? "");

  const selectedConduit = getConduitSync(selectedConduitName, conduits);

  useEffect(() => {
    if (open) {
      setSelectedConduitName(conduitName);
      setInputValues(defaultInputs ?? {});
      setRunPath(defaultRunPath ?? "");
      setName("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleConduitSelect = (cName: string) => {
    setSelectedConduitName(cName);
    const c = getConduitSync(cName, conduits);
    setInputValues(
      c ? Object.fromEntries(Object.keys(c.inputs).map((k) => [k, ""])) : {},
    );
    setConduitPickerOpen(false);
  };

  const handleInputChange = (inputName: string, value: string) => {
    setInputValues((prev) => ({ ...prev, [inputName]: value }));
  };

  const toggleDay = (day: number) => {
    setSelectedDays((prev) =>
      prev.includes(day) ? prev.filter((d) => d !== day) : [...prev, day],
    );
  };

  const updateTime = (index: number, value: string) => {
    setTimes((prev) => prev.map((t, i) => (i === index ? value : t)));
  };

  const addTime = () => setTimes((prev) => [...prev, ""]);

  const removeTime = (index: number) => {
    setTimes((prev) => prev.filter((_, i) => i !== index));
  };

  const handleConfirm = () => {
    const base = { name: name || undefined };
    const inputs = selectedConduit ? submittedInputs(selectedConduit, inputValues) : inputValues;
    if (mode === "once") {
      onConfirm(
        { ...base, mode: "once", runAt: toLocalIso(runAt) },
        selectedConduitName,
        inputs,
        runPath,
      );
    } else {
      onConfirm(
        {
          ...base,
          mode: "recurring",
          days: selectedDays,
          times: times.filter(Boolean),
        },
        selectedConduitName,
        inputs,
        runPath,
      );
    }
    onOpenChange(false);
  };

  const isValid =
    name.trim() !== "" &&
    !!selectedConduitName &&
    (mode === "once"
      ? !!runAt
      : selectedDays.length > 0 && times.some((t) => t !== ""));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-lg sm:w-full">
        <DialogHeader>
          <DialogTitle>
            schedule a{" "}
            <em className="text-primary not-italic">conduit</em>
          </DialogTitle>
          <DialogDescription>
            choose a conduit and when it should run automatically
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto space-y-5 py-2 pr-3">
          {/* Conduit picker */}
          <div className="space-y-2">
            <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
              conduit
            </label>
            <Popover open={conduitPickerOpen} onOpenChange={setConduitPickerOpen}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="flex w-full items-center justify-between border border-border/60 bg-transparent px-2 py-1.5 text-left font-mono text-label text-foreground hover:border-primary focus-visible:outline-2 focus-visible:outline-primary"
                >
                  <span
                    className={
                      selectedConduitName
                        ? "text-foreground"
                        : "text-muted-foreground"
                    }
                  >
                    {selectedConduitName || "select conduit…"}
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
                    onClick={() => handleConduitSelect(c.name)}
                    className={`w-full px-2 py-1.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none ${
                      selectedConduitName === c.name ? "bg-primary/8" : ""
                    }`}
                  >
                    <div
                      className={`font-mono text-label leading-tight ${
                        selectedConduitName === c.name
                          ? "text-primary"
                          : "text-foreground"
                      }`}
                    >
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
          </div>

          {/* Run path */}
          <div className="space-y-2">
            <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
              run path
            </label>
            <input
              value={runPath}
              onChange={(e) => setRunPath(e.target.value)}
              className="w-full border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground focus:border-primary"
              placeholder="/path/to/project"
            />
          </div>

          {/* Inputs */}
          {selectedConduit &&
            Object.keys(selectedConduit.inputs).length > 0 && (
              <div className="space-y-2">
                <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
                  inputs
                </label>
                <div className="border-t border-border/60">
                  {Object.entries(selectedConduit.inputs).map(
                    ([inputName, hint]) => (
                      <div
                        key={inputName}
                        className="grid grid-cols-[120px_1fr] items-start gap-4 border-b border-border/50 py-3"
                      >
                        <div>
                          <div className="font-mono text-label uppercase tracking-[0.12em] text-foreground">
                            {inputName}
                          </div>
                          <div className="mt-1 text-mini text-muted-foreground">
                            {inputHint(hint)}
                          </div>
                        </div>
                        <input
                          value={inputValues[inputName] ?? ""}
                          onChange={(e) =>
                            handleInputChange(inputName, e.target.value)
                          }
                          className="w-full border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground focus:border-primary"
                          placeholder={inputDefault(hint) ?? hintStr(hint)}
                        />
                      </div>
                    ),
                  )}
                </div>
              </div>
            )}

          {/* Mode tabs */}
          <div className="flex border border-border">
            {(["once", "recurring"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={cn(
                  "flex-1 py-2 font-mono text-label uppercase tracking-[0.12em] transition-colors",
                  mode === m
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {m === "once" ? "one-time" : "recurring"}
              </button>
            ))}
          </div>

          {mode === "once" ? (
            <div className="space-y-2">
              <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
                run at
              </label>
              <input
                type="datetime-local"
                value={runAt}
                onChange={(e) => setRunAt(e.target.value)}
                className="w-full border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground focus:border-primary"
              />
            </div>
          ) : (
            <div className="space-y-5">
              <div className="space-y-2">
                <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
                  on days
                </label>
                <div className="flex justify-between gap-1">
                  {DAYS.map(({ value, label }) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => toggleDay(value)}
                      className={cn(
                        "flex h-9 w-9 items-center justify-center rounded-full border text-label font-mono uppercase transition-colors",
                        selectedDays.includes(value)
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border text-muted-foreground hover:border-foreground hover:text-foreground",
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="space-y-2">
                <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
                  at times
                </label>
                <div className="space-y-2">
                  {times.map((time, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <input
                        type="time"
                        value={time}
                        onChange={(e) => updateTime(i, e.target.value)}
                        className="flex-1 border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground focus:border-primary"
                      />
                      {times.length > 1 && (
                        <button
                          type="button"
                          aria-label={`Remove time ${i + 1}`}
                          onClick={() => removeTime(i)}
                          className="flex size-6 shrink-0 items-center justify-center text-muted-foreground transition-colors hover:text-destructive"
                        >
                          <X className="size-3.5" aria-hidden />
                        </button>
                      )}
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={addTime}
                    className="flex h-7 w-7 items-center justify-center rounded border border-dashed border-border text-muted-foreground hover:border-foreground hover:text-foreground transition-colors"
                  >
                    +
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Name */}
          <div className="space-y-2">
            <label className="font-mono text-label uppercase tracking-[0.12em] text-muted-foreground">
              name
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={selectedConduitName}
              className="w-full border-0 border-b border-border-strong bg-transparent pb-2 font-mono text-data text-foreground placeholder:text-muted-foreground focus:border-primary"
            />
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            cancel
          </Button>
          <Button size="sm" onClick={handleConfirm} disabled={!isValid}>
            <Clock className="size-3.5" aria-hidden />
            schedule
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
