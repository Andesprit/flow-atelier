import type { ConduitTask, TaskCondition } from "@/types/conduit";

/**
 * The backend encodes conditional dependencies inside `depends_on` strings:
 *
 *   depends_on: ["run_tests", "code_review.output.match(VERDICT:\\s*APPROVE)"]
 *
 * The designer needs the plain task name to draw an edge between two nodes, so
 * these helpers split that DSL apart on load and put it back together on save.
 * Everything here is a pure string transform — mirror of
 * `flow_atelier/modules/conditions.py`.
 */

const MATCH = ".output.match(";
const NOT_MATCH = ".output.not_match(";

/** One parsed dependency: the source task plus an optional condition. */
export interface ParsedDependency {
  task: string;
  condition?: TaskCondition;
}

/**
 * Split one `depends_on` entry into its source task and condition.
 *
 * A string that is not conditional DSL comes back as a plain task name, so an
 * unrecognised or malformed entry is preserved verbatim rather than dropped.
 */
export function parseDependency(dep: string): ParsedDependency {
  for (const [marker, kind] of [
    [NOT_MATCH, "not_match"],
    [MATCH, "match"],
  ] as const) {
    const idx = dep.indexOf(marker);
    if (idx === -1) continue;
    const task = dep.slice(0, idx);
    const rest = dep.slice(idx + marker.length);
    if (!task || !rest.endsWith(")")) continue;
    const raw = rest.slice(0, -1);
    const pattern = stripSurroundingQuotes(raw);
    const condition: TaskCondition = { kind, pattern };
    // Remember the quoting so `formatDependency` can restore it verbatim.
    if (pattern !== raw) condition.quote = raw[0];
    return { task, condition };
  }
  return { task: dep };
}

/**
 * Drop one wrapping quote pair, matching the engine's leniency so the designer
 * shows the same pattern the engine will actually compile.
 */
export function stripSurroundingQuotes(pattern: string): string {
  const q = pattern[0];
  if (
    pattern.length > 2 &&
    (q === '"' || q === "'") &&
    pattern[pattern.length - 1] === q &&
    pattern[pattern.length - 2] !== "\\"
  ) {
    return pattern.slice(1, -1);
  }
  return pattern;
}

/**
 * Render a source task plus optional condition back into a `depends_on` entry.
 *
 * Restores the quote characters the pattern was parsed with, so loading a
 * conduit and saving it back is a no-op on disk rather than a silent rewrite
 * of YAML the user never touched.
 */
export function formatDependency(task: string, condition?: TaskCondition): string {
  if (!condition) return task;
  const fn = condition.kind === "not_match" ? "output.not_match" : "output.match";
  const q = condition.quote ?? "";
  return `${task}.${fn}(${q}${condition.pattern}${q})`;
}

/**
 * Wire-shaped task (conditions encoded in `depends_on`) -> designer-shaped task
 * (plain `dependsOn` names plus a `conditions` map keyed by source task).
 */
export function fromWireTask(task: ConduitTask): ConduitTask {
  const dependsOn: string[] = [];
  const conditions: Record<string, TaskCondition> = {};

  for (const dep of task.dependsOn ?? []) {
    const { task: source, condition } = parseDependency(dep);
    // `conditions` is keyed by source task, so a task listed twice (say
    // `["a", "a.output.match(X)"]`) cannot keep both entries — the second
    // would overwrite the first and `toWireTask` would then emit the same
    // conditional dep twice. Keep the first occurrence and drop the dupe.
    if (dependsOn.includes(source)) continue;
    dependsOn.push(source);
    if (condition) conditions[source] = condition;
  }

  const out: ConduitTask = { ...task, dependsOn };
  if (Object.keys(conditions).length > 0) out.conditions = conditions;
  else delete out.conditions;
  return out;
}

/**
 * Inverse of {@link fromWireTask}: fold `conditions` back into `dependsOn`.
 *
 * `position` goes with it. Canvas coordinates are designer state, not part of
 * a conduit definition — the backend has never stored them — and it now
 * rejects fields it does not know rather than dropping them, so leaving the
 * key in the payload would fail every save.
 */
export function toWireTask(task: ConduitTask): ConduitTask {
  const { conditions, position, ...rest } = task;
  return {
    ...rest,
    dependsOn: (task.dependsOn ?? []).map((dep) =>
      formatDependency(dep, conditions?.[dep]),
    ),
  };
}

/** Apply {@link fromWireTask} across a task list. */
export function fromWireTasks(tasks: ConduitTask[]): ConduitTask[] {
  return tasks.map(fromWireTask);
}

/** Apply {@link toWireTask} across a task list. */
export function toWireTasks(tasks: ConduitTask[]): ConduitTask[] {
  return tasks.map(toWireTask);
}

/**
 * The task's conditions with the one gating `source` removed, or `undefined`
 * when none remain. Dropping a dependency edge must drop its condition too,
 * otherwise a stale gate lingers on a task that no longer depends on it.
 */
export function withoutCondition(
  task: ConduitTask,
  source: string,
): Record<string, TaskCondition> | undefined {
  if (!task.conditions?.[source]) return task.conditions;
  const next = { ...task.conditions };
  delete next[source];
  return Object.keys(next).length > 0 ? next : undefined;
}

/**
 * Re-key conditions after a task rename. Conditions are keyed by the source
 * task name, so a rename that only rewrote `dependsOn` would strand the gate
 * under the old key and silently turn it into a plain dependency.
 */
export function renameConditionSource(
  conditions: Record<string, TaskCondition> | undefined,
  from: string,
  to: string,
): Record<string, TaskCondition> | undefined {
  if (!conditions?.[from]) return conditions;
  const { [from]: moved, ...rest } = conditions;
  return { ...rest, [to]: moved };
}
