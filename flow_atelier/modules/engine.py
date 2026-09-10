"""Async DAG engine.

Given a parsed Conduit and inputs, the engine:

1. Validates the DAG (unknown deps, cycles, invalid regex).
2. Creates a flow via the store.
3. Runs tasks concurrently. All tasks whose deps are satisfied are launched
   in parallel (bounded by ``conduit.max_concurrency``).
4. Handles ``repeat``, fail-fast, per-task timeout, skip propagation via
   conditional dependencies and templating SkipSignals.

The engine is pure business logic: it holds no knowledge of filesystems or
CLIs beyond the ``StoreBase`` / ``ExecutorBase`` interfaces it is given.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
import traceback
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flow_atelier.modules.conditions import (
    Dependency,
    DependencyParseError,
    evaluate,
    evaluate_loop_predicate,
    parse_dependencies,
    parse_output_predicate,
)
from flow_atelier.modules.liveness import is_crashed
from flow_atelier.modules.templating import (
    SkipSignal,
    TemplateError,
    extract_template_refs,
    resolve,
)
from flow_atelier.schemas.conduit import (
    CONDUIT_NAME_RE,
    Conduit,
    TaskDefinition,
    ToolType,
)
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.log import (
    ExecutionResult,
    LogEntry,
    StepRecord,
    TaskEvent,
)
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskProgress, TaskStatus
from flow_atelier.services.executor.base import ExecutorBase, FlowContext
from flow_atelier.services.executor.bash import to_bash_path
from flow_atelier.services.store.base import StoreBase

TaskEventCallback = Callable[[TaskEvent], None]
FlowStartedCallback = Callable[[str], None]
TaskStartingCallback = Callable[[str, str], None]


class ConduitValidationError(ValueError):
    pass


class ConduitCycleError(ConduitValidationError):
    """Raised when nested ``tool:conduit`` runs form a cycle or exceed depth."""


# Margin added to conduit.timeout for the engine's backstop wait_for, so
# executors that self-enforce ctx.timeout always finish gracefully first.
BACKSTOP_GRACE_SECONDS = 5

# Hard ceiling on nested tool:conduit recursion as a backstop for chains
# that are acyclic-by-name yet still pathologically deep.
MAX_NESTED_CONDUIT_DEPTH = 25


def _now() -> str:
    """Return the current UTC timestamp as an ISO-8601 ``Z`` suffixed string.

    :returns: ISO-8601 timestamp ending with ``Z``.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


_current_task_ctx: ContextVar[str] = ContextVar("current_task_name", default="")
_current_flow_ctx: ContextVar[str] = ContextVar("current_flow_id", default="")


def current_flow_id(default: str = "") -> str:
    """Return the flow id of the run executing on this task (child-aware).

    Public accessor over the internal ``_current_flow_ctx`` ContextVar so
    callers outside this module don't depend on the private name.

    :param default: value to return when no flow context is set.
    :returns: the current flow id, or ``default``.
    """
    return _current_flow_ctx.get(default)


def current_task(default: str = "") -> str:
    """Return the name of the task currently executing on this run.

    Public accessor over the internal ``_current_task_ctx`` ContextVar.

    :param default: value to return when no task context is set.
    :returns: the current task name, or ``default``.
    """
    return _current_task_ctx.get(default)


def accepted_input_keys(conduit: Conduit) -> set[str]:
    """Return every input key ``conduit`` can use.

    A key counts when it is declared under ``inputs:`` or referenced as
    ``{{inputs.<key>}}`` in a task body or a task ``inputs:`` map (a
    ``tool:conduit`` forward). Any other key the engine silently drops, so
    callers reject those before a run starts or a schedule is installed.

    :param conduit: the parsed conduit.
    :returns: the set of input keys the conduit declares or references.
    """
    accepted = set(conduit.inputs)
    for t in conduit.tasks:
        for template in (t.task, *(v for v in t.inputs.values() if isinstance(v, str))):
            accepted |= {r.value for r in extract_template_refs(template) if r.kind == "input"}
    return accepted


def validate_conduit(conduit: Conduit) -> dict[str, list[Dependency]]:
    """Validate the conduit DAG and return its parsed dependency map.

    Two responsibilities, both load-bearing: it *validates* the DAG (unknown
    dependencies, cycles, invalid regexes, stray template references) and it
    *parses* each task's ``depends_on`` into :class:`Dependency` objects, which
    the run loop later relies on. Raises on any validation failure.

    :param conduit: parsed conduit whose tasks/dependencies to validate.
    :returns: mapping of task name to its list of parsed dependencies.
    """
    task_names = {t.name for t in conduit.tasks}
    parsed: dict[str, list[Dependency]] = {}
    for t in conduit.tasks:
        try:
            parsed_deps = parse_dependencies(t.depends_on)
        except DependencyParseError as e:
            raise ConduitValidationError(
                f"task {t.name!r}: {e}"
            ) from e
        for d in parsed_deps:
            if d.task not in task_names:
                raise ConduitValidationError(
                    f"task {t.name!r} depends on unknown task {d.task!r}"
                )
        parsed[t.name] = parsed_deps

    # Cycle detection via DFS
    WHITE, GREY, BLACK = 0, 1, 2
    color = {name: WHITE for name in parsed}

    def visit(name: str, stack: list[str]) -> None:
        """DFS-visit ``name``, raising on grey-revisit cycles.

        :param name: task name to visit next.
        :param stack: current DFS stack used to render cycle paths.
        """
        if color[name] == GREY:
            cycle = " -> ".join(stack[stack.index(name):] + [name])
            raise ConduitValidationError(f"circular dependency: {cycle}")
        if color[name] == BLACK:
            return
        color[name] = GREY
        stack.append(name)
        for d in parsed[name]:
            visit(d.task, stack)
        stack.pop()
        color[name] = BLACK

    for name in parsed:
        visit(name, [])

    # Template refs must point at a (transitive) dependency: resolution
    # order is otherwise a scheduling race — the ref resolves if the
    # other task happens to finish first and silently skips if not.
    closure: dict[str, set[str]] = {}

    def reachable(name: str) -> set[str]:
        """Return the set of task names transitively reachable from ``name``.

        :param name: task whose dependency closure to compute.
        """
        if name not in closure:
            deps = {d.task for d in parsed[name]}
            closure[name] = deps.union(
                *(reachable(d) for d in deps)
            ) if deps else set()
        return closure[name]

    for t in conduit.tasks:
        # A literal tool:conduit target becomes a single filesystem path
        # component at read time; reject traversal at author time. Templated
        # targets are skipped here (resolved/validated at run time).
        if (
            t.tool == ToolType.conduit
            and "{{" not in t.task
            and not CONDUIT_NAME_RE.match(t.task.strip())
        ):
            raise ConduitValidationError(
                f"task {t.name!r}: tool:conduit target {t.task!r} is not a valid "
                "conduit name (letters, digits, '_' and '-' only)"
            )

        allowed = reachable(t.name)
        targets = [t.task]
        if t.tool == ToolType.conduit:
            targets += [v for v in t.inputs.values() if isinstance(v, str)]
        for template in targets:
            for ref in extract_template_refs(template):
                if ref.kind == "task":
                    if ref.value not in task_names:
                        raise ConduitValidationError(
                            f"task {t.name!r} references unknown task "
                            f"{ref.value!r}"
                        )
                    if ref.value not in allowed:
                        raise ConduitValidationError(
                            f"task {t.name!r} references {ref.value!r} which is "
                            f"not in its depends_on chain; add it as a dependency"
                        )
                elif ref.kind == "loop":
                    if t.repeat <= 1:
                        raise ConduitValidationError(
                            f"task {t.name!r} uses {{{{{ref.raw}}}}} but does "
                            f"not loop (repeat is 1); loop.* is only available "
                            f"when repeat > 1"
                        )
                elif ref.kind == "unknown":
                    raise ConduitValidationError(
                        f"task {t.name!r} has an unrecognized template "
                        f"expression {{{{{ref.raw}}}}}"
                    )
                # ref.kind == "input": not validated — inputs may be supplied
                # at run time via --input or HITL, so they need not be declared.

    return parsed


class Engine:
    """Async DAG executor for :class:`Conduit` definitions.

    :param executors: mapping of tool string (e.g. ``"tool:bash"``) to executor
    :param store: :class:`StoreBase` used to read conduits and persist flow state
    """

    def __init__(
        self,
        executors: dict[str, ExecutorBase],
        store: StoreBase,
        loop_history_limit: int = 10,
        loop_history_entry_chars: int = 40000,
    ) -> None:
        """Wire the engine with its executor registry and persistence store.

        :param executors: mapping of tool string to :class:`ExecutorBase`.
        :param store: :class:`StoreBase` used to read conduits and persist flow state.
        :param loop_history_limit: max iterations ``{{loop.history}}`` renders,
            newest kept; <= 0 means unlimited.
        :param loop_history_entry_chars: max characters per rendered
            ``{{loop.history}}`` entry; <= 0 means unlimited.
        """
        self.executors = executors
        self.store = store
        self.loop_history_limit = loop_history_limit
        self.loop_history_entry_chars = loop_history_entry_chars

    # ------------------------------------------------------------------ public

    async def run(
        self,
        conduit: Conduit,
        inputs: dict[str, Any],
        parent_flow_id: str | None = None,
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | None = None,
        flow_id: str | None = None,
        resume_from: str | None = None,
        ancestor_conduits: tuple[str, ...] = (),
        stoppable: bool = False,
        invoking_task: str | None = None,
    ) -> str:
        """Execute a conduit to completion, returning the flow id.

        :param conduit: parsed :class:`Conduit` definition
        :param inputs: conduit input map (must cover all required keys)
        :param parent_flow_id: parent flow id for nested ``tool:conduit`` runs
        :param on_task_event: optional callback invoked after each task
            iteration with a :class:`TaskEvent`; used by the CLI renderer.
        :param on_flow_started: optional callback invoked exactly once with
            the new flow id, immediately after it is created and before any
            task runs. Lets callers (e.g. the CLI) record the id so they
            can surface it even if the flow later fails.
        :param on_task_starting: optional callback invoked once per task as it
            transitions to running for the first time, with ``(task_name, tool)``.
        :param show_steps: whether nested executors should surface per-step
            progress events.
        :param working_dir: working directory for task execution.
        :param flow_id: optional pre-generated flow id; when ``None`` the
            store generates one. Ignored when ``resume_from`` is set.
        :param resume_from: flow id of a prior failed run to resume; skips
            already-completed tasks and reuses their persisted outputs.
        :param ancestor_conduits: names of the conduits already on the nested
            ``tool:conduit`` call stack above this run; used to detect cycles
            and bound recursion depth. Empty for a top-level run.
        :param stoppable: when True and this is a top-level run, install a
            ``SIGTERM`` handler that gracefully cancels the run and finalizes
            it as ``stopped`` (the ``atelier stop`` path). Off by default so
            nested runs and the shared scheduler daemon never hijack SIGTERM.
        :param invoking_task: for nested ``tool:conduit`` runs, the name of the
            parent step that spawned this child; recorded on the child's
            progress so resume can match the right child when a parent has two
            steps invoking the same sub-conduit. ``None`` for top-level runs.
        :returns: the new flow id on success
        :raises ConduitValidationError: DAG is invalid (cycle, unknown dep, bad regex)
        :raises ConduitCycleError: nested conduits form a cycle or exceed depth
        :raises ValueError: required inputs are missing
        :raises Exception: first task failure propagates after fail-fast cancel
        """
        # Guard nested tool:conduit recursion before any flow dir is created:
        # a self- or mutually-referential conduit would otherwise recurse
        # until the host exhausts the stack or the disk fills.
        if conduit.name in ancestor_conduits:
            chain = " -> ".join((*ancestor_conduits, conduit.name))
            raise ConduitCycleError(f"nested conduit cycle detected: {chain}")
        if len(ancestor_conduits) >= MAX_NESTED_CONDUIT_DEPTH:
            chain = " -> ".join((*ancestor_conduits, conduit.name))
            raise ConduitCycleError(
                f"nested conduit depth exceeded {MAX_NESTED_CONDUIT_DEPTH}: {chain}"
            )

        # Absolute dir the conduit loaded from, backing {{conduit_dir}}.
        # None when the conduit isn't registered in the store (e.g. ad-hoc run).
        try:
            conduit_dir = self.store.conduit_dir(conduit.name)
        except FileNotFoundError:
            conduit_dir = None

        # Apply declared defaults, then require inputs that have none.
        # Defaults bypass resolve(), which is single-pass, so a substituted
        # default value is never re-scanned — resolve {{conduit_dir}} here (D5).
        def _apply_default(value: Any) -> Any:
            if conduit_dir is not None and isinstance(value, str):
                return value.replace("{{conduit_dir}}", str(conduit_dir))
            return value

        inputs = {
            **{
                k: _apply_default(spec.default)
                for k, spec in conduit.inputs.items()
                if spec.default is not None
            },
            **inputs,
        }
        missing = [k for k in conduit.inputs if k not in inputs]
        if missing:
            raise ValueError(f"missing required inputs: {missing}")

        parsed_deps = validate_conduit(conduit)

        if resume_from is not None:
            flow_id = resume_from
            prior = self.store.read_progress(flow_id)
            prior_outputs = self.store.read_outputs(flow_id)
        else:
            flow_id = self.store.create_flow(conduit.name, inputs, parent_flow_id, flow_id=flow_id)
        _current_flow_ctx.set(flow_id)
        if on_flow_started is not None and resume_from is None:
            try:
                on_flow_started(flow_id)
            except Exception as cb_exc:  # noqa: BLE001
                # Caller-supplied callback bugs must never break the flow.
                print(
                    f"[flow-atelier] on_flow_started callback raised: "
                    f"{type(cb_exc).__name__}: {cb_exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        # Engine bookkeeping (run dir, invoking step) lives on progress, NOT in
        # input.yaml, so it can never collide with a user-declared input or leak
        # into the {{inputs.*}} namespace on resume. On resume, recover the prior
        # values when this run doesn't supply fresh ones.
        if working_dir:
            run_path = str(working_dir)
        elif resume_from is not None:
            run_path = prior.run_path
        else:
            run_path = None
        if invoking_task is None and resume_from is not None:
            invoking_task = prior.invoking_task

        # Only a top-level stoppable run installs a graceful-stop SIGTERM
        # handler; record that on progress so ``atelier stop`` can refuse flows
        # (daemon/serve/nested) that share or hijack the process's SIGTERM.
        install_stop_handler = stoppable and not ancestor_conduits

        progress = Progress(
            status=FlowStatus.running,
            tasks={
                t.name: TaskProgress(status=TaskStatus.pending, of=t.repeat)
                for t in conduit.tasks
            },
            started_at=_now(),
            runner_pid=os.getpid(),
            runner_host=socket.gethostname(),
            run_path=run_path,
            invoking_task=invoking_task,
            stoppable=install_stop_handler,
        )
        self.store.write_progress(flow_id, progress)

        # Mutable runtime state
        statuses: dict[str, TaskStatus] = {
            t.name: TaskStatus.pending for t in conduit.tasks
        }
        outputs: dict[str, str] = {}
        skip_reasons: dict[str, str] = {}
        task_map = {t.name: t for t in conduit.tasks}

        # Seed state from prior run when resuming
        prior_iterations: dict[str, list[str]] = {}
        if resume_from is not None:
            for tname, tp in prior.tasks.items():
                if tp.status == TaskStatus.completed:
                    statuses[tname] = TaskStatus.completed
                    outputs[tname] = prior_outputs.get(tname, "")
                    progress.tasks[tname] = tp
            self.store.write_progress(flow_id, progress)
            # Rebuild loop history for tasks that will re-run, so they
            # continue at the next iteration with {{loop.*}} context intact.
            # Append order is chronological even across repeated resumes.
            for entry in self.store.read_logs(flow_id):
                if entry.exit_code != 0:
                    continue
                if statuses.get(entry.task) == TaskStatus.completed:
                    continue
                prior_iterations.setdefault(entry.task, []).append(
                    entry.last_turn_output
                    if entry.last_turn_output is not None
                    else entry.output
                )

        runtime_inputs = dict(inputs)  # mutable copy (HITL may append)

        semaphore = asyncio.Semaphore(conduit.max_concurrency)
        running: dict[str, asyncio.Task[None]] = {}
        failed = False
        failure_error: Exception | None = None

        def _safe_emit(event: TaskEvent) -> None:
            """Forward ``event`` to ``on_task_event``, swallowing callback errors.

            :param event: :class:`TaskEvent` to dispatch to the renderer.
            """
            if on_task_event is None:
                return
            try:
                on_task_event(event)
            except Exception as cb_exc:  # noqa: BLE001
                # Renderer bugs must never break the flow.
                print(
                    f"[flow-atelier] on_task_event callback raised: "
                    f"{type(cb_exc).__name__}: {cb_exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        def emit_event(
            t: TaskDefinition,
            iteration: int,
            result: ExecutionResult,
            duration: float,
        ) -> None:
            """Emit a TaskEvent for a completed/failed iteration.

            :param t: task definition that just executed.
            :param iteration: 1-based iteration index within ``t.repeat``.
            :param result: :class:`ExecutionResult` returned by the executor.
            :param duration: wall-clock duration of the iteration in seconds.
            """
            _safe_emit(
                TaskEvent(
                    flow_id=flow_id,
                    task=t.name,
                    tool=t.tool,
                    iteration=iteration,
                    of=t.repeat,
                    exit_code=result.exit_code,
                    duration_seconds=round(duration, 3),
                    output=result.output,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    success=result.success,
                    status=TaskStatus.completed if result.success else TaskStatus.failed,
                    live_streamed=result.live_streamed or t.interactive,
                    steps=result.steps,
                )
            )

        def emit_disposition(
            name: str, status: TaskStatus, reason: str = ""
        ) -> None:
            """Emit a TaskEvent for a non-running disposition (skip/cancel).

            :param name: task name whose disposition is being reported.
            :param status: terminal :class:`TaskStatus` (skipped or cancelled).
            :param reason: optional human-readable reason for the disposition.
            """
            t = task_map[name]
            _safe_emit(
                TaskEvent(
                    flow_id=flow_id,
                    task=t.name,
                    tool=t.tool,
                    of=t.repeat,
                    success=False,
                    status=status,
                    reason=reason,
                )
            )

        def mark_skipped(name: str, reason: str) -> None:
            """Mark a task as skipped, persist progress, and emit a disposition.

            :param name: task name being skipped.
            :param reason: human-readable explanation for the skip.
            """
            statuses[name] = TaskStatus.skipped
            skip_reasons[name] = reason
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.skipped,
                of=task_map[name].repeat,
                reason=reason,
            )
            self.store.write_progress(flow_id, progress)
            emit_disposition(name, TaskStatus.skipped, reason)

        def mark_running(
            name: str, iteration: int, start_iteration: int = 1
        ) -> None:
            """Transition a task to running state and persist progress.

            :param name: task name entering the running state.
            :param iteration: 1-based iteration number about to execute.
            :param start_iteration: first iteration this run executes (> 1
                when resuming); the starting callback fires on it.
            """
            statuses[name] = TaskStatus.running
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.running,
                iteration=iteration,
                of=task_map[name].repeat,
            )
            progress.current_tasks = [
                n for n, s in statuses.items() if s == TaskStatus.running
            ]
            self.store.write_progress(flow_id, progress)
            if iteration == start_iteration and on_task_starting is not None:
                try:
                    on_task_starting(name, task_map[name].tool)
                except Exception:  # noqa: BLE001
                    pass

        def mark_completed(
            name: str, iteration: int, reason: str = ""
        ) -> None:
            """Mark a task as completed at ``iteration`` and persist progress.

            :param name: task name that has finished successfully.
            :param iteration: 1-based iteration number that completed the task.
            :param reason: optional note recorded on the task progress
                (e.g. loop exhaustion without a predicate match).
            """
            statuses[name] = TaskStatus.completed
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.completed,
                iteration=iteration,
                of=task_map[name].repeat,
                reason=reason or None,
            )
            progress.current_tasks = [
                n for n, s in statuses.items() if s == TaskStatus.running
            ]
            self.store.write_progress(flow_id, progress)

        def mark_failed(name: str, reason: str = "") -> None:
            """Mark a task as failed and persist progress.

            :param name: task name that has failed.
            :param reason: human-readable explanation for the failure,
                surfaced in the status ``reason`` column (empty -> None).
            """
            statuses[name] = TaskStatus.failed
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.failed,
                of=task_map[name].repeat,
                reason=reason or None,
            )
            self.store.write_progress(flow_id, progress)

        async def run_task(t: TaskDefinition) -> None:
            """Execute one task end-to-end including repeats and loop predicates.

            :param t: task definition to execute.
            """
            nonlocal failed, failure_error
            _current_task_ctx.set(t.name)
            _current_flow_ctx.set(flow_id)
            try:
                # Resolve {{task.output}} templates now (inputs resolved per-iteration)
                unavailable = {
                    n for n, s in statuses.items()
                    if s in (TaskStatus.skipped, TaskStatus.failed, TaskStatus.cancelled)
                }
                loop_history: list[str] = list(prior_iterations.get(t.name, []))
                start_iteration = min(len(loop_history) + 1, t.repeat)

                # {{conduit_dir}} is engine-computed in the host namespace; a
                # tool:bash body may run under WSL/git-bash (Windows), where a
                # raw `D:\...` path is mangled. Translate it to the resolved
                # bash's namespace for bash tasks only — harness/nested tasks
                # run native on the host and want the host path unchanged.
                task_conduit_dir: Path | str | None = conduit_dir
                if conduit_dir is not None and t.tool == ToolType.bash:
                    task_conduit_dir = to_bash_path(conduit_dir)

                def _resolve_task() -> str:
                    return resolve(
                        t.task, runtime_inputs, outputs,
                        unavailable_tasks=unavailable, loop_history=loop_history,
                        loop_history_limit=self.loop_history_limit,
                        loop_history_entry_chars=self.loop_history_entry_chars,
                        conduit_dir=task_conduit_dir,
                    )

                try:
                    resolved = _resolve_task()
                except SkipSignal as e:
                    mark_skipped(t.name, str(e))
                    return
                except TemplateError as e:
                    reason = str(e)
                    mark_failed(t.name, reason)
                    if not failed:
                        failed = True
                        failure_error = ValueError(f"task {t.name!r}: {reason}")
                    return

                executor = self.executors.get(t.tool)
                if executor is None:
                    reason = f"no executor registered for tool {t.tool!r}"
                    mark_failed(t.name, reason)
                    if not failed:
                        failed = True
                        failure_error = ValueError(reason)
                    return

                # An explicit per-task timeout supersedes the conduit-wide
                # ceiling for this task only; None inherits conduit.timeout.
                effective_timeout = (
                    t.timeout if t.timeout is not None else conduit.timeout
                )

                ctx = FlowContext(
                    flow_id=flow_id,
                    store=self.store,
                    inputs=runtime_inputs,
                    task_outputs=outputs,
                    timeout=effective_timeout,
                    working_dir=working_dir,
                    show_steps=show_steps,
                    run_nested_conduit=self._make_nested_runner(
                        on_task_event,
                        on_flow_started=on_flow_started,
                        on_task_starting=on_task_starting,
                        show_steps=show_steps,
                        working_dir=working_dir,
                        ancestor_conduits=(*ancestor_conduits, conduit.name),
                        invoking_task=t.name,
                    ),
                    loop_history=loop_history,
                    loop_history_limit=self.loop_history_limit,
                    loop_history_entry_chars=self.loop_history_entry_chars,
                    conduit_dir=conduit_dir,
                )

                def _make_step_sink(iteration: int):
                    """Build the per-iteration live step persistence hook.

                    :param iteration: 1-based iteration the steps belong to.
                    :returns: coroutine function appending one step to the store.
                    """

                    async def _persist(step) -> None:
                        """Append one live step record for this iteration.

                        :param step: the :class:`IntermediateStep` to persist.
                        """
                        await self.store.append_step(
                            flow_id,
                            StepRecord(task=t.name, iteration=iteration, step=step),
                        )

                    return _persist

                # Pre-parse the loop predicate once per task. Schema enforces
                # that at most one of `until` / `while_` is set, so we end up
                # with a single (compiled_pattern, negate) plus a mode tag.
                # Already validated at conduit-load time, so this cannot raise
                # in practice.
                loop_predicate: tuple | None = None
                loop_mode: str = "until"
                if t.until is not None:
                    loop_predicate = parse_output_predicate(t.until)
                    loop_mode = "until"
                elif t.while_ is not None:
                    loop_predicate = parse_output_predicate(t.while_)
                    loop_mode = "while"

                # HITL waits on a human: it must not hold a concurrency
                # slot (starving parallel-ready tasks) nor be killed by
                # the backstop timeout while the user is away.
                is_hitl = t.tool == ToolType.hitl

                async with (contextlib.nullcontext() if is_hitl else semaphore):
                    last_output = ""
                    predicate_matched = False
                    # Identical-output streak over this run only; seeded
                    # resume history must not insta-trip the guard.
                    stagnant_streak = 0
                    for iteration in range(start_iteration, t.repeat + 1):
                        # Rebound per iteration so persisted steps carry the
                        # iteration they actually belong to.
                        ctx.on_step = _make_step_sink(iteration)
                        if iteration > start_iteration:
                            try:
                                resolved = _resolve_task()
                            except (SkipSignal, TemplateError) as e:
                                # Mid-loop the task has already produced
                                # output, so a silent skip would lie: fail.
                                reason = f"iteration {iteration}: {e}"
                                mark_failed(t.name, reason)
                                if not failed:
                                    failed = True
                                    failure_error = ValueError(
                                        f"task {t.name!r} {reason}"
                                    )
                                return
                        mark_running(t.name, iteration, start_iteration)
                        # A transient non-zero exit is re-attempted in place up
                        # to `retries` extra times before tripping fail-fast.
                        # HITL is exempt: a human declining a prompt is not a
                        # transient failure, so it never auto-retries.
                        max_attempts = 1 if is_hitl else 1 + t.retries
                        for attempt in range(1, max_attempts + 1):
                            started = _now()
                            start_ts = datetime.now(UTC)
                            try:
                                # Grace margin: executors that self-enforce
                                # ctx.timeout (bash, harness) return a graceful
                                # result preserving output; this outer wrapper is
                                # the backstop for those that don't, and must not
                                # win the race against them. HITL is exempt: a
                                # human stepping away is not a timeout.
                                result = await asyncio.wait_for(
                                    executor.execute(t, resolved, ctx),
                                    timeout=(
                                        None
                                        if is_hitl
                                        else effective_timeout + BACKSTOP_GRACE_SECONDS
                                    ),
                                )
                            except TimeoutError:
                                result = ExecutionResult(
                                    exit_code=124,
                                    stderr=f"engine timeout after {effective_timeout}s",
                                )
                            except Exception as exc:  # noqa: BLE001
                                result = ExecutionResult(
                                    exit_code=1, stderr=f"{type(exc).__name__}: {exc}"
                                )
                            finished = _now()
                            duration = (
                                datetime.now(UTC) - start_ts
                            ).total_seconds()
                            # Only stamp attempt metadata when retries are
                            # actually in play; with no retries the log entry
                            # stays byte-for-byte identical to pre-retry behavior.
                            attempt_extra = (
                                {"attempt": attempt, "of_attempts": max_attempts}
                                if max_attempts > 1
                                else {}
                            )
                            await self.store.append_log(
                                flow_id,
                                LogEntry(
                                    task=t.name,
                                    tool=t.tool,
                                    iteration=iteration,
                                    of=t.repeat,
                                    command=resolved,
                                    stdout=result.stdout,
                                    stderr=result.stderr,
                                    exit_code=result.exit_code,
                                    output=result.output,
                                    last_turn_output=result.last_turn_output,
                                    started_at=started,
                                    finished_at=finished,
                                    duration_seconds=round(duration, 3),
                                    extra=attempt_extra,
                                    steps=result.steps,
                                    usage=result.usage,
                                ),
                            )
                            emit_event(t, iteration, result, duration)
                            if result.success:
                                break
                            if attempt < max_attempts:
                                await asyncio.sleep(t.retry_backoff)
                                continue
                            reason = (
                                f"exit={result.exit_code} "
                                f"stderr={result.stderr.strip()[:200]}"
                            )
                            mark_failed(t.name, reason)
                            if not failed:
                                failed = True
                                failure_error = RuntimeError(
                                    f"task {t.name!r} failed: {reason}"
                                )
                            return
                        previous_output = last_output
                        last_output = (
                            result.last_turn_output
                            if result.last_turn_output is not None
                            else result.output
                        )
                        loop_history.append(last_output)
                        if t.stagnation_limit is not None:
                            if (
                                iteration > start_iteration
                                and last_output == previous_output
                            ):
                                stagnant_streak += 1
                            else:
                                stagnant_streak = 1
                            if stagnant_streak >= t.stagnation_limit:
                                reason = (
                                    f"stagnated: {stagnant_streak} identical "
                                    "consecutive outputs"
                                )
                                mark_failed(t.name, reason)
                                if not failed:
                                    failed = True
                                    failure_error = RuntimeError(
                                        f"task {t.name!r} {reason}"
                                    )
                                return
                        if loop_predicate is not None:
                            # Conduit tasks evaluate the predicate against the
                            # outputs of every nested sub-task (any-match),
                            # not just the conduit's aggregate result.output.
                            if t.tool == ToolType.conduit:
                                scope_outputs = result.sub_outputs
                            else:
                                # Match against the last turn only (same value
                                # history stores): full output can echo prior
                                # iterations via {{loop.history}} and
                                # false-positive the predicate.
                                scope_outputs = [last_output]
                            if await evaluate_loop_predicate(
                                loop_predicate, scope_outputs, loop_mode
                            ):
                                predicate_matched = True
                                break
                    completion_reason = ""
                    if loop_predicate is not None and not predicate_matched:
                        if t.on_exhaust == "fail":
                            reason = (
                                f"exhausted {t.repeat} iterations without "
                                "matching its loop predicate"
                            )
                            mark_failed(t.name, reason)
                            if not failed:
                                failed = True
                                failure_error = RuntimeError(
                                    f"task {t.name!r} {reason}"
                                )
                            return
                        completion_reason = (
                            "loop exhausted without predicate match"
                        )
                    outputs[t.name] = last_output
                    mark_completed(t.name, iteration, reason=completion_reason)
                    self.store.write_outputs(flow_id, outputs)
            except asyncio.CancelledError:
                if statuses[t.name] not in (
                    TaskStatus.completed, TaskStatus.failed, TaskStatus.skipped
                ):
                    statuses[t.name] = TaskStatus.cancelled
                    progress.tasks[t.name] = TaskProgress(
                        status=TaskStatus.cancelled, of=t.repeat
                    )
                    self.store.write_progress(flow_id, progress)
                    emit_disposition(
                        t.name, TaskStatus.cancelled, "fail-fast: upstream failed"
                    )
                raise

        # ------------------------------------------------------------------ loop
        # Install a SIGTERM handler for a stoppable top-level run so
        # ``atelier stop`` can trigger the same graceful cancel path that
        # Ctrl-C does, but finalize the flow as ``stopped`` rather than
        # ``failed``. Only the outermost run arms it: nested runs and the
        # shared scheduler daemon must keep their own SIGTERM semantics.
        stop_requested = False
        sigterm_installed = False
        active_run_task = asyncio.current_task()
        if install_stop_handler:
            loop = asyncio.get_running_loop()

            def _request_stop() -> None:
                nonlocal stop_requested
                stop_requested = True
                if active_run_task is not None:
                    active_run_task.cancel()

            try:
                loop.add_signal_handler(signal.SIGTERM, _request_stop)
                sigterm_installed = True
            except (NotImplementedError, RuntimeError):
                sigterm_installed = False
        try:
            while True:
                # Evaluate all pending tasks; launch satisfied, skip unsatisfiable.
                for name, t in task_map.items():
                    if statuses[name] != TaskStatus.pending:
                        continue
                    if failed:
                        break
                    deps = parsed_deps[name]
                    decision = "satisfied"
                    skip_reason: str | None = None
                    for d in deps:
                        r, reason = await evaluate(d, statuses, outputs)
                        if r == "skip":
                            decision = "skip"
                            skip_reason = reason
                            break
                        if r == "wait":
                            decision = "wait"
                            break
                    if decision == "skip":
                        mark_skipped(name, skip_reason or "dependency not met")
                    elif decision == "satisfied":
                        statuses[name] = TaskStatus.running  # reserve
                        running[name] = asyncio.create_task(run_task(t))

                # Termination check
                pending_exists = any(
                    s == TaskStatus.pending for s in statuses.values()
                )
                if not running and not pending_exists:
                    break
                if failed and not running:
                    break

                # Wait for at least one task transition
                if running:
                    done, _pending = await asyncio.wait(
                        list(running.values()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for d in done:
                        name_done = next(n for n, task in running.items() if task is d)
                        running.pop(name_done)
                        # propagate exceptions only for engine bugs; task bodies trap them
                        exc = d.exception()
                        if (
                            exc
                            and not isinstance(exc, asyncio.CancelledError)
                            and not failed
                        ):
                            failed = True
                            failure_error = exc
                else:
                    # No running tasks but still pending — every pending task
                    # waits on something that can no longer change. Engine bug;
                    # fail loud instead of hanging forever.
                    raise RuntimeError(
                        "engine stalled: pending tasks but nothing running"
                    )

                if failed and running:
                    for rt in running.values():
                        rt.cancel()
                    await asyncio.gather(*running.values(), return_exceptions=True)
                    running.clear()

            # Mark any still-pending tasks (due to fail-fast) as cancelled
            for name, s in statuses.items():
                if s == TaskStatus.pending:
                    statuses[name] = TaskStatus.cancelled
                    progress.tasks[name] = TaskProgress(
                        status=TaskStatus.cancelled, of=task_map[name].repeat
                    )
                    emit_disposition(
                        name, TaskStatus.cancelled, "upstream failed"
                    )

            progress.current_tasks = []
            progress.finished_at = _now()
            progress.status = FlowStatus.failed if failed else FlowStatus.completed
            self.store.write_progress(flow_id, progress)

            if not failed:
                self.store.write_outputs(
                    flow_id,
                    {name: outputs.get(name) for name in task_map},
                )

            if failed:
                raise failure_error or RuntimeError("flow failed")
            return flow_id
        except BaseException:
            # Cancel running tasks so child flows get cleaned up and
            # their progress is written as failed instead of left as running.
            if running:
                for rt in running.values():
                    rt.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
            # Ensure progress reflects a terminal state. A deliberate stop
            # (SIGTERM via ``atelier stop``) finalizes as ``stopped``; every
            # other interruption (crash, Ctrl-C, task error) stays ``failed``.
            progress.current_tasks = []
            progress.finished_at = _now()
            if progress.status == FlowStatus.running:
                progress.status = (
                    FlowStatus.stopped if stop_requested else FlowStatus.failed
                )
            self.store.write_progress(flow_id, progress)
            raise
        finally:
            if sigterm_installed:
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.remove_signal_handler(signal.SIGTERM)

    # ------------------------------------------------------------------ helpers

    def _find_child_to_resume(
        self, parent_flow_id: str, conduit_name: str, invoking_task: str | None
    ) -> str | None:
        """Find the most recent resumable child flow for a conduit.

        Matches a ``failed`` child unconditionally, and a ``running`` child only
        when its runner is *provably* dead (:func:`is_crashed`) — a child left
        ``running`` is either an orphan from a crashed parent (resumable) or a
        flow still in flight, and picking up a live one would drive the same
        child directory from two engines at once. A child is considered only
        when it was spawned by the same parent step (``invoking_task``), so a
        parent with two distinct steps invoking the same sub-conduit resumes the
        child belonging to the step being re-run rather than the most recent
        child of that name.

        :param parent_flow_id: parent flow to search under
        :param conduit_name: child conduit name to match
        :param invoking_task: name of the parent step whose child to match
        :returns: child flow id to resume, or None
        """
        for fid in reversed(self.store.list_child_flows(parent_flow_id)):
            try:
                cname, _, _ = parse_flow_id(fid)
            except ValueError:
                continue
            if cname != conduit_name:
                continue
            try:
                p = self.store.read_progress(fid)
            except (FileNotFoundError, ValueError):
                continue
            if p.invoking_task != invoking_task:
                continue
            if p.status == FlowStatus.failed:
                return fid
            if p.status == FlowStatus.running:
                # A still-live child must not be double-driven; only resume one
                # whose runner is provably dead.
                return fid if is_crashed(p) else None
            return None  # most recent child for this step already completed
        return None

    def _make_nested_runner(
        self,
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | None = None,
        ancestor_conduits: tuple[str, ...] = (),
        invoking_task: str | None = None,
    ):
        """Build the nested-conduit runner passed to executors via FlowContext.

        :param on_task_event: optional task-event callback forwarded to the child run.
        :param on_flow_started: optional flow-started callback forwarded to the child.
        :param on_task_starting: optional task-starting callback forwarded to the child.
        :param show_steps: whether the nested run should surface per-step progress.
        :param working_dir: working directory forwarded to nested runs.
        :param ancestor_conduits: conduit names already on the nested-run stack,
            forwarded to each child run so cycles/depth are caught.
        :param invoking_task: name of the parent step this runner belongs to;
            bound per-task so the executor-facing callback signature stays
            ``(conduit_name, child_inputs, parent_flow_id)``. Used to record and
            match the child against the right parent step on resume.
        :returns: an async callable ``(conduit_name, child_inputs, parent_flow_id)``
            that loads and runs the named child conduit.
        """
        async def _run_nested(conduit_name: str, child_inputs, parent_flow_id):
            """Load and run a child conduit, returning its flow id.

            :param conduit_name: name of the child conduit to load from the store.
            :param child_inputs: input map passed to the child conduit.
            :param parent_flow_id: flow id of the parent run, for linkage.
            """
            child_conduit = self.store.read_conduit(conduit_name)

            # Resume an existing failed child if one exists
            resume_id = self._find_child_to_resume(
                parent_flow_id, conduit_name, invoking_task
            )
            if resume_id is not None:
                prior_inputs = self.store.read_input(resume_id)
                return await self.run(
                    child_conduit,
                    prior_inputs,
                    parent_flow_id,
                    resume_from=resume_id,
                    on_task_event=on_task_event,
                    on_flow_started=on_flow_started,
                    on_task_starting=on_task_starting,
                    show_steps=show_steps,
                    working_dir=working_dir,
                    ancestor_conduits=ancestor_conduits,
                    invoking_task=invoking_task,
                )

            return await self.run(
                child_conduit,
                child_inputs,
                parent_flow_id,
                on_task_event=on_task_event,
                on_flow_started=on_flow_started,
                on_task_starting=on_task_starting,
                show_steps=show_steps,
                working_dir=working_dir,
                ancestor_conduits=ancestor_conduits,
                invoking_task=invoking_task,
            )

        return _run_nested
