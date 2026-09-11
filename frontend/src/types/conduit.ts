/** Tools backed by flow-atelier's own executors. A closed set. */
export type BuiltinToolType = "tool:bash" | "tool:hitl" | "tool:conduit";

/**
 * Any ACP agent, named by its ACP-registry id (`harness:gemini`) or by a
 * command the user declared in `ATELIER_HARNESSES`. Open by design: the
 * backend resolves the name against its executor registry, so pinning this
 * to a fixed list here would reject conduits the engine runs happily.
 * `TOOL_META` still enumerates the ones the designer offers by default.
 */
export type HarnessToolType = `harness:${string}`;

export type ToolType = BuiltinToolType | HarnessToolType;

/** A gate on one dependency edge: run only if the source output (not) matches. */
export interface TaskCondition {
  kind: "match" | "not_match";
  pattern: string;
  /**
   * Quote character the pattern was written with in `depends_on`, when it had
   * one. The engine strips quotes before compiling, so the designer shows the
   * bare pattern — but it puts them back on save, otherwise merely opening a
   * conduit and saving it rewrites the author's YAML.
   */
  quote?: string;
}

export interface ConduitTask {
  name: string;
  tool: ToolType;
  description: string;
  task: string;
  dependsOn: string[];
  /**
   * Conditions keyed by the source task they gate, so a task can be gated on
   * several dependencies at once. Encoded into `dependsOn` strings on the wire
   * (see `@/utils/conditions`); this shape exists only inside the designer.
   */
  conditions?: Record<string, TaskCondition>;
  interactive?: boolean;
  repeat?: number;
  inputs?: Record<string, string>;
  position?: { x: number; y: number };
}

export interface CreateConduitRequest {
  name: string;
  description: string;
  inputs: Record<string, string | InputSpec>;
  timeout?: number;
  maxConcurrency?: number;
  interaction?: InteractionPolicy | null;
  tasks: ConduitTask[];
}

export interface InputSpec {
  description: string;
  default?: string | null;
}

/** Extract a display string from a conduit input hint (string or InputSpec). */
export function hintStr(hint: string | InputSpec): string {
  return typeof hint === "string" ? hint : hint.description;
}

/** The declared default of a conduit input, when it has one. */
export function inputDefault(hint: string | InputSpec): string | undefined {
  return typeof hint === "string" ? undefined : hint.default ?? undefined;
}

/** Hint line for an input field: its description, and its default when it has one. */
export function inputHint(hint: string | InputSpec): string {
  const def = inputDefault(hint);
  return [hintStr(hint), def == null ? "" : `default: ${def}`].filter(Boolean).join(" · ");
}

/**
 * What to send for a run, schedule or task: every value the user typed, and
 * nothing for an input left empty that has a default. The engine fills those
 * in (and resolves `{{conduit_dir}}` inside a default, which the UI cannot);
 * sending "" instead would override the default with an empty string.
 */
export function submittedInputs(
  conduit: Pick<Conduit, "inputs">,
  values: Record<string, string>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, hint] of Object.entries(conduit.inputs)) {
    const value = values[name] ?? "";
    if (value.trim() || inputDefault(hint) == null) out[name] = value;
  }
  return out;
}

/** Slugify a task name: replace non-alphanumeric chars with underscores. */
export function slugifyTaskName(name: string): string {
  return name.replace(/[^A-Za-z0-9_]+/g, "_");
}

export interface Conduit {
  name: string;
  description: string;
  timeout?: number;
  maxConcurrency?: number;
  interaction?: InteractionPolicy | null;
  runPath?: string;
  inputs: Record<string, string | InputSpec>;
  tasks: ConduitTask[];
}

/** Conduit-wide routing for harness conversation and tool permissions. */
export interface InteractionPolicy {
  questions: "human" | "supervisor" | "hybrid";
  permissions: "approve_all" | "human" | "supervisor" | "hybrid";
  supervisor?: {
    tool: string;
    instructions?: string;
    maxReplies?: number;
    timeout?: number;
    maxContextChars?: number;
  } | null;
}
