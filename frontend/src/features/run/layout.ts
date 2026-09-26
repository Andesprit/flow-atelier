import { layerTasks } from "@/features/designer/layout";
import type { RunTask } from "@/types/run";

export const NODE_W = 168;
export const NODE_H = 56;
const GAP_X = 40;
const GAP_Y = 16;
// Room above the first row for the "you are here" label.
const PAD_TOP = 36;
const PAD = 20;

export interface MapNode {
  name: string;
  x: number;
  y: number;
}

export interface MapEdge {
  from: string;
  to: string;
  d: string;
}

export interface RunMapLayout {
  nodes: MapNode[];
  edges: MapEdge[];
  width: number;
  height: number;
}

/**
 * Place each task one column right of its deepest dependency, the same
 * layering the designer and `atelier plan` use, and route each dependency as a
 * curve from the right edge of one node to the left edge of the next.
 */
export function layoutRunMap(tasks: RunTask[]): RunMapLayout {
  const depth = layerTasks(tasks);
  const rowsInColumn = new Map<number, number>();
  const nodes: MapNode[] = [];
  for (const task of tasks) {
    const col = depth.get(task.name) ?? 0;
    const row = rowsInColumn.get(col) ?? 0;
    rowsInColumn.set(col, row + 1);
    nodes.push({
      name: task.name,
      x: PAD + col * (NODE_W + GAP_X),
      y: PAD_TOP + row * (NODE_H + GAP_Y),
    });
  }

  const byName = new Map(nodes.map((n) => [n.name, n]));
  const edges: MapEdge[] = [];
  for (const task of tasks) {
    const to = byName.get(task.name);
    for (const dep of task.dependsOn) {
      const from = byName.get(dep);
      if (!from || !to || from === to) continue;
      const x1 = from.x + NODE_W;
      const y1 = from.y + NODE_H / 2;
      const x2 = to.x;
      const y2 = to.y + NODE_H / 2;
      const mid = (x1 + x2) / 2;
      edges.push({ from: dep, to: task.name, d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}` });
    }
  }

  const cols = Math.max(0, ...depth.values()) + 1;
  const rows = Math.max(1, ...rowsInColumn.values());
  return {
    nodes,
    edges,
    width: PAD * 2 + cols * NODE_W + (cols - 1) * GAP_X,
    height: PAD_TOP + PAD + rows * NODE_H + (rows - 1) * GAP_Y,
  };
}

/** The box that frames the tasks running now, padded so it clears their borders. */
export function hereBox(nodes: MapNode[], current: string[]) {
  const here = nodes.filter((n) => current.includes(n.name));
  if (here.length === 0) return undefined;
  const m = 10;
  const x = Math.min(...here.map((n) => n.x)) - m;
  const y = Math.min(...here.map((n) => n.y)) - m;
  return {
    x,
    y,
    width: Math.max(...here.map((n) => n.x + NODE_W)) + m - x,
    height: Math.max(...here.map((n) => n.y + NODE_H)) + m - y,
  };
}
