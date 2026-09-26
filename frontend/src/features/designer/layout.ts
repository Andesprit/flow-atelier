import type { ConduitTask } from "@/types/conduit";

/**
 * Longest-path layering: each task sits one column right of its deepest
 * dependency, so a column holds tasks that could run in the same wave. Matches
 * how `atelier plan` renders the DAG on the CLI.
 *
 * Returns task name -> column index.
 */
export function layerTasks(tasks: Pick<ConduitTask, "name" | "dependsOn">[]): Map<string, number> {
  const byName = new Map(tasks.map((t) => [t.name, t]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  const depthOf = (name: string): number => {
    const cached = depth.get(name);
    if (cached !== undefined) return cached;
    // A cycle or a dangling dependency name anchors at column 0 rather than
    // recursing forever. The engine rejects both, but the designer edits YAML
    // that may not be valid yet, and a layout pass must never hang the UI.
    if (visiting.has(name)) return 0;
    const task = byName.get(name);
    if (!task) return 0;

    visiting.add(name);
    const deps = task.dependsOn.filter((d) => byName.has(d) && d !== name);
    const own = deps.length ? Math.max(...deps.map(depthOf)) + 1 : 0;
    visiting.delete(name);

    depth.set(name, own);
    return own;
  };

  for (const t of tasks) depthOf(t.name);
  return depth;
}

/**
 * Grid position for every task: column from {@link layerTasks}, row from the
 * order tasks appear within their column.
 */
export function layoutPositions(
  tasks: ConduitTask[],
  stepX: number,
  stepY: number,
  origin = 80,
): Map<string, { x: number; y: number }> {
  const depth = layerTasks(tasks);
  const nextRow = new Map<number, number>();
  const out = new Map<string, { x: number; y: number }>();

  for (const task of tasks) {
    const col = depth.get(task.name) ?? 0;
    const row = nextRow.get(col) ?? 0;
    nextRow.set(col, row + 1);
    out.set(task.name, { x: origin + col * stepX, y: origin + row * stepY });
  }
  return out;
}
