"""Static execution-plan view — a pure transform of a validated conduit.

Turns the ``{task_name: [parsed deps]}`` map that
:func:`flow_atelier.modules.engine.validate_conduit` already produces into an
ordered, annotated picture of a DAG: wave levels, plain vs. conditional
(skip) edges, loop predicates, sinks, and the short-circuit gates whose
non-matching output prunes a downstream subtree. No I/O, no executor, no
runtime state — structural only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flow_atelier.modules.conditions import (
    ConditionalDependency,
    PlainDependency,
    sink_task_names,
)
from flow_atelier.schemas.conduit import Conduit


@dataclass(frozen=True)
class PlannedEdge:
    """One incoming dependency of a task, split by kind.

    A conditional edge is a *skip-edge*: if its regex misses the source
    task's output at run time, the dependent (and everything reachable only
    through it) is skipped.
    """

    task: str
    conditional: bool
    pattern: str | None = None
    negate: bool = False


@dataclass
class PlannedTask:
    name: str
    tool: str
    level: int
    # The tool the installed recipe names, when this plan's ``tool`` came from
    # a per-task agent selection instead. ``None`` means the two agree.
    recipe_tool: str | None = None
    plain_edges: list[PlannedEdge] = field(default_factory=list)
    conditional_edges: list[PlannedEdge] = field(default_factory=list)
    is_loop: bool = False
    loop_text: str | None = None
    is_sink: bool = False
    is_gate: bool = False
    prunes: list[str] = field(default_factory=list)
    # True when ``--worktree`` selected this task, so a run would give it its
    # own Git checkout instead of the shared working directory.
    isolated: bool = False


@dataclass
class PlanIsolation:
    """What ``--worktree`` would do, without doing any of it.

    ``source`` and ``base`` are what a run started here *now* would cut the
    checkouts from; ``problem`` is set instead when it could not, so the
    preview names the refusal before a run pays for it.
    """

    tasks: list[str]
    source: str | None = None
    base: str | None = None
    problem: str | None = None


@dataclass
class ExecutionPlan:
    conduit_name: str
    max_concurrency: int
    waves: list[list[PlannedTask]]
    sinks: list[str]
    isolation: PlanIsolation | None = None


def _loop_text(task) -> str | None:
    """Render a one-line loop badge for a ``repeat > 1`` task, else ``None``.

    :param task: the :class:`TaskDefinition` to describe.
    :returns: a string like ``x10 until output.match(VERDICT:\\s*DONE)`` or
        ``None`` when the task does not loop.
    """
    if task.repeat <= 1:
        return None
    parts = [f"x{task.repeat}"]
    if task.until is not None:
        parts.append(f"until {task.until}")
    elif task.while_ is not None:
        parts.append(f"while {task.while_}")
    if task.stagnation_limit is not None:
        parts.append(f"stagnation_limit={task.stagnation_limit}")
    if task.on_exhaust != "complete":
        parts.append(f"on_exhaust={task.on_exhaust}")
    return " ".join(parts)


def build_plan(
    conduit: Conduit,
    parsed: dict[str, list],
    recipe: Conduit | None = None,
    isolation: PlanIsolation | None = None,
) -> ExecutionPlan:
    """Build a static :class:`ExecutionPlan` from a validated conduit.

    ``parsed`` is the map returned by
    :func:`flow_atelier.modules.engine.validate_conduit`; the conduit is
    assumed already validated (acyclic, deps resolvable), so no cycle guard
    is needed here.

    :param conduit: the parsed conduit, with any per-task agent selections
        already applied — so every ``tool`` here is the effective one.
    :param parsed: ``{task_name: [parsed deps]}`` from ``validate_conduit``.
    :param recipe: the same conduit as installed, when ``conduit`` carries
        selections; each differing tool is reported as the recipe's default so
        the plan cannot be mistaken for the file on disk.
    :param isolation: the ``--worktree`` preview, when any task was selected.
    :returns: the ordered, annotated execution plan.
    """
    isolated = set(isolation.tasks) if isolation is not None else set()
    installed = {t.name: t.tool for t in recipe.tasks} if recipe is not None else {}
    # Longest-path layering: level = 0 for roots, else 1 + max(dep levels).
    # Iterative post-order (parsed is already validated acyclic) so a deep
    # single-chain conduit cannot blow the recursion limit.
    level: dict[str, int] = {}
    for start in parsed:
        if start in level:
            continue
        stack = [start]
        while stack:
            name = stack[-1]
            if name in level:
                stack.pop()
                continue
            deps = parsed[name]
            pending = [d.task for d in deps if d.task not in level]
            if pending:
                stack.extend(pending)
                continue
            level[name] = 0 if not deps else 1 + max(level[d.task] for d in deps)
            stack.pop()

    sinks = set(sink_task_names(conduit))

    # A gate is any task that is the source of a conditional edge. If its
    # regex misses, every direct conditional dependent is skipped, and skip
    # propagates through ALL downstream edges (the engine skips a task when
    # any of its deps is skipped). So a gate's prune set is the transitive
    # dependents of its direct conditional dependents.
    dependents: dict[str, list[str]] = {name: [] for name in parsed}
    cond_dependents: dict[str, list[str]] = {name: [] for name in parsed}
    for name, deps in parsed.items():
        for d in deps:
            dependents[d.task].append(name)
            if isinstance(d, ConditionalDependency):
                cond_dependents[d.task].append(name)

    def prune_set(gate: str) -> list[str]:
        pruned: set[str] = set()
        stack = list(cond_dependents[gate])
        while stack:
            t = stack.pop()
            if t in pruned:
                continue
            pruned.add(t)
            stack.extend(dependents[t])
        return [t.name for t in conduit.tasks if t.name in pruned]

    planned: dict[str, PlannedTask] = {}
    for t in conduit.tasks:
        plain_edges: list[PlannedEdge] = []
        conditional_edges: list[PlannedEdge] = []
        for d in parsed[t.name]:
            if isinstance(d, ConditionalDependency):
                conditional_edges.append(
                    PlannedEdge(
                        task=d.task,
                        conditional=True,
                        pattern=d.pattern,
                        negate=d.negate,
                    )
                )
            else:
                assert isinstance(d, PlainDependency)
                plain_edges.append(PlannedEdge(task=d.task, conditional=False))
        is_gate = bool(cond_dependents[t.name])
        planned[t.name] = PlannedTask(
            name=t.name,
            tool=t.tool,
            level=level[t.name],
            recipe_tool=(
                installed[t.name]
                if installed.get(t.name, t.tool) != t.tool
                else None
            ),
            plain_edges=plain_edges,
            conditional_edges=conditional_edges,
            is_loop=t.repeat > 1,
            loop_text=_loop_text(t),
            is_sink=t.name in sinks,
            is_gate=is_gate,
            prunes=prune_set(t.name) if is_gate else [],
            isolated=t.name in isolated,
        )

    max_level = max(level.values()) if level else 0
    waves: list[list[PlannedTask]] = [[] for _ in range(max_level + 1)]
    for t in conduit.tasks:  # preserve definition order within each wave
        waves[level[t.name]].append(planned[t.name])

    return ExecutionPlan(
        conduit_name=conduit.name,
        max_concurrency=conduit.max_concurrency,
        waves=waves,
        sinks=[t.name for t in conduit.tasks if t.name in sinks],
        isolation=isolation,
    )
