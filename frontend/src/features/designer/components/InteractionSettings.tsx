import { useId, useRef } from "react";
import { ChevronDown } from "lucide-react";
import type { InteractionPolicy } from "@/types/conduit";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { interactionError, needsSupervisor } from "../interaction-policy";

const modeLabels = {
  human: "Human always",
  supervisor: "Supervisor always",
  hybrid: "Supervisor can ask me",
  approve_all: "Approve all",
} as const;
const questionModes = ["human", "supervisor", "hybrid"] as const;
const permissionModes = ["approve_all", ...questionModes] as const;
const labelClass = "text-micro tracking-[0.14em]";

export function InteractionSettings({ value, onChange }: {
  value?: InteractionPolicy | null;
  onChange: (value: InteractionPolicy | null) => void;
}) {
  const id = useId();
  const previousPolicy = useRef<InteractionPolicy | null>(null);
  const previousSupervisor = useRef<InteractionPolicy["supervisor"]>(null);
  const supervised = needsSupervisor(value);
  const error = interactionError(value);
  const update = (patch: Partial<InteractionPolicy>) => {
    const next: InteractionPolicy = { questions: "human", permissions: "approve_all", ...value, ...patch };
    if (next.supervisor) previousSupervisor.current = next.supervisor;
    if (needsSupervisor(next)) {
      next.supervisor ??= previousSupervisor.current ?? { tool: "harness:codex", instructions: "" };
    } else {
      // Inactive supervisor settings must not block a human-only policy save.
      delete next.supervisor;
    }
    onChange(next);
  };

  return (
    <details className="group border-t border-border/60 px-4 py-3">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 rounded-sm focus-visible:outline-2 focus-visible:outline-primary [&::-webkit-details-marker]:hidden">
        <span className="min-w-0">
          <span className="block font-mono text-micro uppercase tracking-[0.14em] text-muted-foreground">interaction</span>
          <span className="mt-1 block text-mini text-foreground">
            {value ? <>
              <span className="block">Questions: {modeLabels[value.questions]}</span>
              <span className="block">Tool approvals: {modeLabels[value.permissions]}</span>
            </> : "Task defaults"}
          </span>
          {error && <span className="block text-mini text-destructive">Check supervisor name</span>}
        </span>
        <ChevronDown className="size-4 shrink-0 text-muted-foreground group-open:rotate-180" aria-hidden />
      </summary>
      <div className="space-y-4 pt-3" role="group" aria-label="Conduit interaction">
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-label">
          <input type="checkbox" className="size-4 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-primary"
            checked={!!value} aria-describedby={`${id}-scope`}
            onChange={(e) => {
              if (e.target.checked) onChange(previousPolicy.current ?? { questions: "human", permissions: "approve_all" });
              else { previousPolicy.current = value ?? null; onChange(null); }
            }} />
          Use conduit policy
        </label>
        <p id={`${id}-scope`} className="text-mini leading-relaxed text-muted-foreground">
          {value ? "Applies to all harness tasks. Human gates stay unchanged."
            : "Tasks use their own conversation settings. Tool requests are approved automatically."}
        </p>
        {value && <>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-questions`} className={labelClass}>Questions</Label>
            <Select value={value.questions} onValueChange={(mode) => {
              const valid = questionModes.find((option) => option === mode);
              if (valid) update({ questions: valid });
            }}>
              <SelectTrigger id={`${id}-questions`} className="h-11 text-mini"><SelectValue /></SelectTrigger>
              <SelectContent>{questionModes.map((mode) => (
                <SelectItem key={mode} value={mode} className="h-11 text-mini">{modeLabels[mode]}</SelectItem>
              ))}</SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-permissions`} className={labelClass}>Tool approvals</Label>
            <Select value={value.permissions} onValueChange={(mode) => {
              const valid = permissionModes.find((option) => option === mode);
              if (valid) update({ permissions: valid });
            }}>
              <SelectTrigger id={`${id}-permissions`} className="h-11 text-mini"><SelectValue /></SelectTrigger>
              <SelectContent>{permissionModes.map((mode) => (
                <SelectItem key={mode} value={mode} className="h-11 text-mini">{modeLabels[mode]}</SelectItem>
              ))}</SelectContent>
            </Select>
          </div>
          {supervised && <>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-harness`} className={labelClass}>Supervisor harness</Label>
              <Input id={`${id}-harness`} className="h-11 text-mini" value={value.supervisor?.tool ?? ""}
                placeholder="harness:codex" autoComplete="off" spellCheck={false}
                aria-invalid={!!error} aria-describedby={error ? `${id}-error` : undefined}
                onChange={(e) => update({ supervisor: { ...value.supervisor, tool: e.target.value } })} />
              {error && <p id={`${id}-error`} role="status" className="text-mini text-destructive">{error}</p>}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-instructions`} className={labelClass}>Instructions</Label>
              <textarea id={`${id}-instructions`}
                className="min-h-28 w-full resize-y rounded-sm border border-border bg-transparent px-3 py-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-primary"
                value={value.supervisor?.instructions ?? ""}
                placeholder="Which decisions should the supervisor make?"
                onChange={(e) => update({ supervisor: {
                  ...value.supervisor, tool: value.supervisor?.tool ?? "harness:codex", instructions: e.target.value,
                } })} />
            </div>
            <p className="text-mini leading-relaxed text-muted-foreground">
              The supervisor reads the session before deciding. “Always” stops the task if it cannot decide; “can ask me” hands the question to you.
            </p>
          </>}
        </>}
      </div>
    </details>
  );
}
