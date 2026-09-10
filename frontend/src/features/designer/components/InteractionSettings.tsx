import { useId } from "react";
import type { InteractionPolicy } from "@/types/conduit";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const fieldClass = "min-h-11 w-full rounded-md border border-border bg-background px-2 text-label text-foreground focus-visible:outline-2 focus-visible:outline-primary";

export function InteractionSettings({ value, onChange }: {
  value?: InteractionPolicy | null;
  onChange: (value: InteractionPolicy | null) => void;
}) {
  const id = useId();
  const supervised = [value?.questions, value?.permissions].some(
    (mode) => mode === "supervisor" || mode === "hybrid",
  );
  const update = (patch: Partial<InteractionPolicy>) => {
    const next: InteractionPolicy = { questions: "human", permissions: "approve_all", ...value, ...patch };
    if ([next.questions, next.permissions].some((mode) => mode === "supervisor" || mode === "hybrid")) {
      next.supervisor ??= { tool: "harness:codex", instructions: "" };
    }
    onChange(next);
  };

  return (
    <section className="space-y-3 border-t border-border/60 px-4 py-5" aria-label="Conduit interaction">
      <h3 className="font-mono text-micro uppercase tracking-widest text-muted-foreground">interaction</h3>
      <label className="flex min-h-11 items-center gap-2 text-label">
        <input type="checkbox" checked={!!value} onChange={(e) => onChange(
          e.target.checked ? { questions: "human", permissions: "approve_all" } : null,
        )} />
        Set behavior for this conduit
      </label>
      <p className="text-mini leading-relaxed text-muted-foreground">
        {value ? "Applies to every harness task. Human approval gates remain human."
          : "Uses each task’s interactive setting and approves tool operations automatically."}
      </p>
      {value && <>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-questions`}>Who answers questions?</Label>
          <select id={`${id}-questions`} className={fieldClass} value={value.questions}
            onChange={(e) => update({ questions: e.target.value as InteractionPolicy["questions"] })}>
            <option value="human">Human always</option>
            <option value="supervisor">Supervisor always</option>
            <option value="hybrid">Supervisor, with escalation</option>
          </select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-permissions`}>Who approves tool operations?</Label>
          <select id={`${id}-permissions`} className={fieldClass} value={value.permissions}
            onChange={(e) => update({ permissions: e.target.value as InteractionPolicy["permissions"] })}>
            <option value="approve_all">Approve all</option>
            <option value="human">Human always</option>
            <option value="supervisor">Supervisor always</option>
            <option value="hybrid">Supervisor, with escalation</option>
          </select>
        </div>
        {supervised && <>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-harness`}>Supervisor harness</Label>
            <Input id={`${id}-harness`} value={value.supervisor?.tool ?? ""}
              placeholder="harness:codex" onChange={(e) => update({ supervisor: {
                ...value.supervisor, tool: e.target.value,
              } })} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-instructions`}>Supervisor instructions</Label>
            <textarea id={`${id}-instructions`} className={`${fieldClass} min-h-28 py-2`}
              value={value.supervisor?.instructions ?? ""}
              placeholder="Describe which decisions it should make or escalate."
              onChange={(e) => update({ supervisor: {
                ...value.supervisor!, instructions: e.target.value,
              } })} />
          </div>
          <p className="text-mini leading-relaxed text-muted-foreground">
            The supervisor receives the full session exposed by the harness.
            “Supervisor always” fails if it cannot answer; escalation modes ask you.
          </p>
        </>}
      </>}
    </section>
  );
}
