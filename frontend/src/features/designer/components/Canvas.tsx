import { useCallback, useState, useEffect, useRef, useMemo, type ReactNode } from "react";
import { MousePointer2, Hand, Maximize2, LayoutGrid } from "lucide-react";
import {
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  applyNodeChanges,
  applyEdgeChanges,
  MarkerType,
  MiniMap,
  type Node,
  type Edge,
  type Connection,
  type OnNodesChange,
  type OnEdgesChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Conduit, ConduitTask } from "@/types/conduit";
import { TaskNode, type TaskNodeData } from "./TaskNode";
import { DepEdge, type DepEdgeData } from "./DepEdge";
import { CanvasRulers } from "./CanvasRulers";
import { EdgePopupContext, type PopupInfo } from "../edge-popup-context";
import { EdgeTypePopup, type EdgeKind } from "./EdgeTypePopup";
import { layoutPositions } from "../layout";
import { withoutCondition } from "@/utils/conditions";

const nodeTypes = { task: TaskNode };
const edgeTypes = { dep: DepEdge };

const DEFAULT_EXTENT: [[number, number], [number, number]] = [[0, 0], [3000, 2000]];
const EXTENT_PAD = 600;
const NODE_W = 220;
const NODE_H = 120;
const GRID_GAP_X = 60;
const GRID_GAP_Y = 60;

interface CanvasProps {
  conduit: Conduit;
  onSelect: (task: ConduitTask | undefined) => void;
  onInspect?: (task: ConduitTask) => void;
  onUpdateTask: (name: string, partial: Partial<ConduitTask>) => void;
  onDeleteTask: (name: string) => void;
  onShowTools?: () => void;
  positionRef?: React.MutableRefObject<((tool: string) => { x: number; y: number }) | null>;
}

export function Canvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function CanvasInner({ conduit, onSelect, onInspect, onUpdateTask, onDeleteTask, onShowTools, positionRef }: CanvasProps) {
  const rf = useReactFlow();
  const canvasRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(100);
  const [containerSize, setContainerSize] = useState({ w: 800, h: 600 });
  const [popup, setPopup] = useState<PopupInfo | null>(null);
  const [mode, setMode] = useState<"select" | "pan">("select");
  const [spaceHeld, setSpaceHeld] = useState(false);
  const pendingConnectionRef = useRef<{ source: string; target: string } | null>(null);
  const dragPositionsRef = useRef<Map<string, { x: number; y: number }>>(new Map());

  const conduitRef = useRef(conduit);
  conduitRef.current = conduit;

  const activeMode = spaceHeld ? "pan" : mode;

  useEffect(() => {
    const onDown = (e: KeyboardEvent) => {
      if (e.code === "Space" && !e.repeat && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)) {
        e.preventDefault();
        setSpaceHeld(true);
      }
    };
    const onUp = (e: KeyboardEvent) => {
      if (e.code === "Space") setSpaceHeld(false);
    };
    window.addEventListener("keydown", onDown);
    window.addEventListener("keyup", onUp);
    return () => {
      window.removeEventListener("keydown", onDown);
      window.removeEventListener("keyup", onUp);
    };
  }, []);

  // Expose a position helper so the parent can place new nodes relative to the
  // viewport. Assigned in an effect (not during render) so React can safely
  // discard/replay renders without capturing stale values.
  useEffect(() => {
    if (!positionRef) return;
    positionRef.current = (_tool: string) => {
      const { x: vx, y: vy, zoom: z } = rf.getViewport();
      const rect = canvasRef.current?.getBoundingClientRect();
      const cw = rect?.width ?? 800;
      const ch = rect?.height ?? 600;
      // Center of viewport in flow coordinates
      const cx = (-vx + cw / 2) / z;
      const cy = (-vy + ch / 2) / z;
      // Offset to avoid stacking on existing nodes
      const existing = conduitRef.current.tasks;
      const offset = existing.length * 40;
      return { x: Math.round(cx - NODE_W / 2 + offset), y: Math.round(cy - NODE_H / 2) };
    };
  }, [positionRef, rf]);

  const toNodes = (c: Conduit): Node<TaskNodeData>[] =>
    c.tasks.map((t, i) => ({
      id: t.name,
      type: "task",
      position: t.position ?? { x: 80 + i * 240, y: 140 },
      data: {
        idx: i + 1,
        name: t.name,
        tool: t.tool,
        task: t.task,
        description: t.description,
        repeat: t.repeat,
        until: t.until,
        while: t.while,
        onExhaust: t.onExhaust,
        conditional: Object.values(t.conditions ?? {})[0]?.kind,
      },
    }));

  const [nodes, setNodes] = useState<Node<TaskNodeData>[]>(() => toNodes(conduit));
  useEffect(() => {
    setNodes((prev) => {
      const selectedIds = new Set(prev.filter((n) => n.selected).map((n) => n.id));
      const next = toNodes(conduit);
      if (selectedIds.size === 0) return next;
      return next.map((n) => (selectedIds.has(n.id) ? { ...n, selected: true } : n));
    });
  }, [conduit]);

  const onNodesChange: OnNodesChange<Node<TaskNodeData>> = useCallback((changes) => {
    // Track position changes during drag for multi-select sync
    for (const change of changes) {
      if (change.type === "position" && change.position) {
        dragPositionsRef.current.set(change.id, change.position);
      }
    }
    setNodes((nds) => applyNodeChanges(changes, nds));
  }, []);

  const toEdges = (c: Conduit): Edge<DepEdgeData>[] => {
    const taskNames = new Set(c.tasks.map((t) => t.name));
    const out: Edge<DepEdgeData>[] = [];
    for (const t of c.tasks) {
      for (const dep of t.dependsOn) {
        if (!taskNames.has(dep)) continue;
        const conditional = t.conditions?.[dep]?.kind;
        const edgeKind: EdgeKind = conditional ?? "depends_on";
        // Was "black", which is invisible against the dark theme's background.
        const color = conditional
          ? conditional === "match"
            ? "var(--color-primary)"
            : "var(--color-destructive)"
          : "var(--color-edge)";
        out.push({
          id: `${dep}->${t.name}`,
          source: dep,
          target: t.name,
          type: "dep",
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 18,
            height: 18,
            color,
          },
          data: {
            conditional,
            edgeKind,
          },
        });
      }
    }
    return out;
  };

  const [edges, setEdges] = useState<Edge<DepEdgeData>[]>(() => toEdges(conduit));
  useEffect(() => setEdges(toEdges(conduit)), [conduit]);

  const onEdgesChange: OnEdgesChange = useCallback((changes) => {
    // Only apply non-structural changes (select). Edge structure (add/remove)
    // is derived from conduit state — applying those here causes ghost edges.
    const safe = changes.filter((c) => c.type === "select");
    if (safe.length > 0) {
      setEdges((eds) => applyEdgeChanges(safe, eds));
    }
  }, []);

  const extent = useMemo((): [[number, number], [number, number]] => {
    if (nodes.length === 0) return DEFAULT_EXTENT;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      const w = n.measured?.width ?? NODE_W;
      const h = n.measured?.height ?? NODE_H;
      minX = Math.min(minX, n.position.x);
      minY = Math.min(minY, n.position.y);
      maxX = Math.max(maxX, n.position.x + w);
      maxY = Math.max(maxY, n.position.y + h);
    }
    return [
      [minX - EXTENT_PAD, minY - EXTENT_PAD],
      [maxX + EXTENT_PAD, maxY + EXTENT_PAD],
    ];
  }, [nodes]);

  // Track container dimensions for dynamic minZoom
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setContainerSize({
        w: entry.contentRect.width,
        h: entry.contentRect.height,
      });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // minZoom so the entire extent fits in the viewport
  const minZoom = useMemo(() => {
    const extentW = extent[1][0] - extent[0][0];
    const extentH = extent[1][1] - extent[0][1];
    if (extentW <= 0 || extentH <= 0) return 0.05;
    return Math.min(containerSize.w / extentW, containerSize.h / extentH, 1);
  }, [extent, containerSize]);

  useEffect(() => {
    if (conduit.tasks.length >= 2) {
      const id = setTimeout(() => rf.fitView({ padding: 0.2, duration: 300 }), 60);
      return () => clearTimeout(id);
    } else {
      rf.setViewport({ x: 80, y: 60, zoom: 1 }, { duration: 300 });
    }
  }, [conduit.name, rf]);

  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      if (activeMode === "pan") return;
      onSelect(conduit.tasks.find((t) => t.name === node.id));
    },
    [conduit, onSelect, activeMode],
  );

  const onNodeDoubleClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      const task = conduit.tasks.find((t) => t.name === node.id);
      if (task && onInspect) onInspect(task);
    },
    [conduit, onInspect],
  );

  const onPaneClick = useCallback(() => {
    onSelect(undefined);
  }, [onSelect]);

  const onNodeDragStop = useCallback(
    (_: React.MouseEvent, _node: Node) => {
      // Flush all tracked positions (handles multi-select drag)
      for (const [id, position] of dragPositionsRef.current) {
        onUpdateTask(id, { position });
      }
      dragPositionsRef.current.clear();
    },
    [onSelect, onUpdateTask],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      const target = conduitRef.current.tasks.find(
        (t) => t.name === connection.target,
      );
      if (!target) return;
      if (target.dependsOn.includes(connection.source)) return;
      pendingConnectionRef.current = {
        source: connection.source,
        target: connection.target,
      };
      onUpdateTask(connection.target, {
        dependsOn: [...target.dependsOn, connection.source],
      });
    },
    [onUpdateTask],
  );

  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent) => {
      if (!pendingConnectionRef.current) return;
      const { source, target } = pendingConnectionRef.current;
      pendingConnectionRef.current = null;
      const clientX = "clientX" in event ? event.clientX : 0;
      const clientY = "clientY" in event ? event.clientY : 0;
      setPopup({ source, target, x: clientX, y: clientY, current: "depends_on", isNew: true });
    },
    [],
  );

  const handlePopupSelect = useCallback(
    (kind: EdgeKind) => {
      if (!popup) return;
      const { source, target } = popup;
      const task = conduitRef.current.tasks.find((t) => t.name === target);
      if (!task) { setPopup(null); return; }

      if (kind === "depends_on") {
        onUpdateTask(target, { conditions: withoutCondition(task, source) });
      } else {
        const existing = task.conditions?.[source]?.pattern ?? "";
        onUpdateTask(target, {
          conditions: { ...task.conditions, [source]: { kind, pattern: existing } },
        });
      }
      setPopup(null);
    },
    [popup, onUpdateTask],
  );

  const onNodesDelete = useCallback(
    (deleted: Node[]) => {
      for (const node of deleted) {
        onDeleteTask(node.id);
      }
    },
    [onDeleteTask],
  );

  const onEdgesDelete = useCallback(
    (deleted: Edge[]) => {
      for (const edge of deleted) {
        const target = conduitRef.current.tasks.find(
          (t) => t.name === edge.target,
        );
        if (!target) continue;
        onUpdateTask(edge.target, {
          dependsOn: target.dependsOn.filter((d) => d !== edge.source),
          conditions: withoutCondition(target, edge.source),
        });
      }
    },
    [onUpdateTask],
  );

  const onReconnect = useCallback(
    (oldEdge: Edge, newConnection: Connection) => {
      if (!newConnection.source || !newConnection.target) return;
      const oldTarget = conduitRef.current.tasks.find(
        (t) => t.name === oldEdge.target,
      );
      if (oldTarget) {
        onUpdateTask(oldEdge.target as string, {
          dependsOn: oldTarget.dependsOn.filter((d) => d !== oldEdge.source),
          conditions: withoutCondition(oldTarget, oldEdge.source),
        });
      }
      const newTarget = conduitRef.current.tasks.find(
        (t) => t.name === newConnection.target,
      );
      if (newTarget && !newTarget.dependsOn.includes(newConnection.source)) {
        onUpdateTask(newConnection.target, {
          dependsOn: [...newTarget.dependsOn, newConnection.source],
        });
      }
    },
    [onUpdateTask],
  );

  const onEdgeClick = useCallback(
    (_: React.MouseEvent, edge: Edge) => {
      const task = conduitRef.current.tasks.find((t) => t.name === edge.target);
      const conditional = task?.conditions?.[edge.source as string]?.kind;
      const edgeKind: EdgeKind = conditional ?? "depends_on";
      const rect = document
        .querySelector(`[data-id="${edge.id}"]`)
        ?.getBoundingClientRect();
      setPopup({
        source: edge.source as string,
        target: edge.target as string,
        x: rect ? rect.left + rect.width / 2 : 0,
        y: rect ? rect.top : 0,
        current: edgeKind,
      });
    },
    [],
  );

  const handleOrganize = useCallback(() => {
    // Lay out by dependency depth, not insertion order: a fixed-width grid
    // wraps unrelated tasks onto the same row and drops dependents to the far
    // left, which reads as a scrambled graph rather than a pipeline.
    const positions = layoutPositions(
      conduitRef.current.tasks,
      NODE_W + GRID_GAP_X,
      NODE_H + GRID_GAP_Y,
    );
    const updated = nodes.map((node) => ({
      ...node,
      position: positions.get(node.id) ?? node.position,
    }));
    setNodes(updated);
    for (const node of updated) {
      onUpdateTask(node.id, { position: node.position });
    }
    setTimeout(() => rf.fitView({ padding: 0.2, duration: 300 }), 50);
  }, [nodes, onUpdateTask, rf]);

  return (
    <EdgePopupContext.Provider
      value={(info) => setPopup(info)}
    >
      <div className="relative h-full w-full overflow-hidden bg-background">
        <CanvasRulers />
        <div
          ref={canvasRef}
          data-testid="designer-canvas"
          className="relative z-[2] h-full w-full"
          style={{ "--xy-background-color": "transparent" } as React.CSSProperties}
        >
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodeClick={onNodeClick}
            onNodeDoubleClick={onNodeDoubleClick}
            onPaneClick={onPaneClick}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onConnectEnd={onConnectEnd}
            onNodesDelete={onNodesDelete}
            onEdgesDelete={onEdgesDelete}
            onEdgeClick={onEdgeClick}
            onReconnect={onReconnect}
            // onMoveEnd, not onMove: onMove fires every frame of a pan or zoom,
            // so re-rendering the whole canvas from it just to refresh a
            // percentage label cost a render per frame for the entire gesture.
            onMoveEnd={() => setZoom(Math.round(rf.getZoom() * 100))}
            onNodeDragStop={onNodeDragStop}
            edgesReconnectable
            zoomOnDoubleClick={false}
            defaultViewport={{ x: 80, y: 60, zoom: 1 }}
            proOptions={{ hideAttribution: true }}
            panOnScroll
            zoomOnScroll
            panOnDrag={activeMode === "pan"}
            selectionOnDrag={activeMode === "select"}
            nodesDraggable={activeMode === "select"}
            minZoom={minZoom}
            maxZoom={1.5}
            nodeOrigin={[0, 0]}
            translateExtent={extent}
            nodeExtent={extent}
          >
            {/* Hidden below lg: at 200x160 it ate nearly half a phone
                viewport. `--background` was not a token in this project
                (`--color-background` is), so the surface fell back to
                react-flow's own default. */}
            {/* `--color-border` measures 1.5:1 on the card surface, so the
                node blocks were nearly invisible and the map read as an empty
                grey panel taking 220x160 of canvas. Muted-foreground (7.4:1
                here) makes it legible, and the smaller footprint gives the
                canvas back the space the map wasn't earning. */}
            <MiniMap
              pannable
              zoomable
              className="!hidden lg:!block"
              style={{ backgroundColor: "var(--color-card)", width: 160, height: 110 }}
              maskColor="color-mix(in oklch, var(--color-background) 55%, transparent)"
              nodeColor="var(--color-muted-foreground)"
            />
          </ReactFlow>
        </div>

        {conduit.tasks.length === 0 && (
          <div className="absolute inset-0 z-[3] flex items-center justify-center">
            <button
              type="button"
              onClick={onShowTools}
              className="flex flex-col items-center gap-2 rounded-md border border-border px-8 py-5 text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
            >
              <span className="text-head leading-none">+</span>
              <span className="font-mono text-body">add first task</span>
            </button>
          </div>
        )}

        {/* Bottom edge, not top: at lg the top strip is occupied by the
            Designer action bar, and the two overlapped so `fit` and `organize`
            were unreachable. The minimap holds the bottom-left corner. */}
        {/* Grouped into one panel so that when the row wraps on a phone it
            still reads as a single toolbar rather than loose buttons. */}
        <div
          data-testid="canvas-toolbar"
          className="absolute inset-x-3 bottom-3 z-dropdown flex flex-wrap items-center justify-center gap-1 rounded-sm border border-border bg-card/80 p-1 backdrop-blur lg:inset-x-auto lg:right-4 lg:bottom-4 lg:gap-1.5"
        >
          <ToolbarButton
            icon={<MousePointer2 className="size-3.5" aria-hidden />}
            label="select"
            active={mode === "select"}
            onClick={() => setMode("select")}
          />
          <ToolbarButton
            icon={<Hand className="size-3.5" aria-hidden />}
            label="pan"
            active={mode === "pan"}
            onClick={() => setMode("pan")}
          />
          <ToolbarButton
            icon={<Maximize2 className="size-3.5" aria-hidden />}
            label="fit"
            onClick={() => rf.fitView({ padding: 0.2, duration: 200 })}
          />
          <ToolbarButton
            icon={<LayoutGrid className="size-3.5" aria-hidden />}
            label="organize"
            onClick={handleOrganize}
          />
          <ToolbarButton label="+" onClick={() => rf.zoomIn({ duration: 150 })} />
          <ToolbarButton label="−" onClick={() => rf.zoomOut({ duration: 150 })} />
          <ToolbarButton
            label={`${zoom}%`}
            onClick={() => rf.zoomTo(1, { duration: 200 })}
          />
        </div>
      </div>

      {popup && (
        <EdgeTypePopup
          x={popup.x}
          y={popup.y}
          current={popup.current}
          onSelect={handlePopupSelect}
          onRemove={() => {
            const task = conduitRef.current.tasks.find((t) => t.name === popup.target);
            if (task) {
              onUpdateTask(popup.target, {
                dependsOn: task.dependsOn.filter((d) => d !== popup.source),
                conditions: withoutCondition(task, popup.source),
              });
            }
            setPopup(null);
          }}
          onClose={() => {
            if (popup.isNew) {
              const task = conduitRef.current.tasks.find((t) => t.name === popup.target);
              if (task) {
                onUpdateTask(popup.target, {
                  dependsOn: task.dependsOn.filter((d) => d !== popup.source),
                });
              }
            }
            setPopup(null);
          }}
        />
      )}
    </EdgePopupContext.Provider>
  );
}

// `icon` is decorative and aria-hidden; `label` is the accessible name. The
// labels used to be strings like "↖ select", which put the glyph inside the
// name — a screen reader announced "north west arrow, select".
function ToolbarButton({
  icon,
  label,
  onClick,
  active,
}: {
  icon?: ReactNode;
  label: string;
  onClick: () => void;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "flex h-11 min-w-11 items-center justify-center gap-1.5 border px-2.5 font-mono text-mini uppercase tracking-[0.12em] lg:tracking-[0.14em] " +
        (active
          ? "border-primary bg-primary/10 text-primary"
          : "border-border bg-card text-foreground hover:border-primary")
      }
    >
      {icon}
      {label}
    </button>
  );
}
