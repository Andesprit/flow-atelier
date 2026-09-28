"""Facade: wires store + executors + engine and exposes the public API."""
from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Container, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.binding import (
    BindingError,
    bind_agent_paths,
    check_replaceable,
    normalize_bindings,
)
from flow_atelier.modules.engine import (
    Engine,
    FlowStartedCallback,
    TaskEventCallback,
    TaskStartingCallback,
    check_unknown_inputs,
    resolve_executor,
    validate_conduit,
)
from flow_atelier.modules.flow_view import build_flow_view, build_task_log
from flow_atelier.modules.liveness import is_runner_alive
from flow_atelier.modules.workspace import (
    check_record,
    check_selectors,
    check_unrecorded,
    create_worktrees,
    resolve_source_repo,
    verify_worktrees,
)
from flow_atelier.schemas.api import (
    CreateConduitInput,
    CreateScheduleInput,
    FlowView,
    PriorFlow,
    RunTaskInput,
    RunTaskOutput,
    ScheduledJob,
    TaskLogView,
    UpdateConduitInput,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.flow import new_flow_id, parse_flow_id
from flow_atelier.schemas.log import LogEntry
from flow_atelier.schemas.progress import (
    FlowStatus,
    Progress,
    TaskStatus,
    Workspaces,
)
from flow_atelier.services.executor.acp_registry import (
    LEGACY_HARNESS_ALIASES,
    SNAPSHOT_FILENAME,
    load_registry,
)
from flow_atelier.services.executor.bash import BashExecutor
from flow_atelier.services.executor.conduit import ConduitExecutor
from flow_atelier.services.executor.harness import AcpHarnessExecutor
from flow_atelier.services.executor.hitl import HitlExecutor
from flow_atelier.services.executor.prompt_sink import PromptSink, TerminalPromptSink
from flow_atelier.services.package import (
    InstallReport,
    PackageError,
    RemoveReport,
    delete_lockfile_entry,
    fetch_source,
    install_package,
    read_lockfile,
    read_package,
    resolve_source,
    write_lockfile,
)
from flow_atelier.services.scheduler.store import ScheduleStore
from flow_atelier.services.store.filesystem import FilesystemStore

logger = logging.getLogger(__name__)


class Atelier:
    """Top-level facade for the flow-atelier engine.

    Wires :class:`FilesystemStore`, the tool/harness executors, and the DAG
    :class:`Engine` together and exposes the public API used by the CLI.

    :param settings: explicit :class:`AtelierSettings`; if omitted, loads
        from environment / ``.env``
    :param base_dir: convenience override for ``settings.atelier_dir``;
        ignored when ``settings`` is passed explicitly
    """

    def __init__(
        self,
        settings: AtelierSettings | None = None,
        base_dir: Path | str | None = None,
        prompt_sink: PromptSink | None = None,
    ) -> None:
        """Construct the facade, wiring store, executors, engine and schedule store.

        :param settings: explicit :class:`AtelierSettings`; if omitted, loads
            from environment / ``.env``.
        :param base_dir: convenience override for ``settings.atelier_dir``;
            ignored when ``settings`` is passed explicitly.
        :param prompt_sink: optional :class:`PromptSink` shared by harness
            executors; defaults to :class:`TerminalPromptSink`.
        """
        if settings is None:
            settings = (
                AtelierSettings(atelier_dir=Path(base_dir))
                if base_dir is not None
                else AtelierSettings()
            )
        self.settings = settings
        self.store = FilesystemStore(
            self.settings.atelier_dir,
            global_dir=self.settings.global_atelier_dir,
        )
        sink: PromptSink = prompt_sink if prompt_sink is not None else TerminalPromptSink()
        self.executors = {
            "tool:bash": BashExecutor(),
            "tool:hitl": HitlExecutor(),
            "tool:conduit": ConduitExecutor(),
        }
        self._register_harnesses(sink)
        self.engine = Engine(
            self.executors,
            self.store,
            loop_history_limit=self.settings.loop_history_limit,
            loop_history_entry_chars=self.settings.loop_history_entry_chars,
        )
        self.schedule_store = ScheduleStore(self.settings.atelier_dir)

    def _register_harnesses(self, sink: PromptSink) -> None:
        """Register a ``harness:<name>`` executor for every known ACP agent.

        Four layers, each overriding the last:

        1. every agent in the ACP registry snapshot, under its registry id;
        2. the legacy names flow-atelier shipped before the registry existed;
        3. the per-harness ``ATELIER_*_LAUNCH_CMD`` argv overrides;
        4. ``ATELIER_HARNESSES``, for agents the registry has never heard of.

        :param sink: :class:`PromptSink` shared by every harness executor.
        """
        def make(argv: list[str], env: dict[str, str] | None = None) -> AcpHarnessExecutor:
            """Build a harness executor for ``argv``.

            :param argv: the agent's launch command.
            :param env: optional extra environment variables.
            """
            return AcpHarnessExecutor(
                launch_cmd=argv,
                sink=sink,
                done_marker=self.settings.done_marker,
                env=env,
            )

        self.registry = load_registry(self.settings.global_atelier_dir / SNAPSHOT_FILENAME)
        for agent_id, spec in self.registry.items():
            self.executors[f"harness:{agent_id}"] = make(list(spec.argv), spec.env)
        for alias, target in LEGACY_HARNESS_ALIASES.items():
            spec = self.registry.get(target)
            if spec is not None:
                self.executors[f"harness:{alias}"] = make(list(spec.argv), spec.env)
        launch_overrides = {
            "claude-code": self.settings.claude_launch_cmd,
            "codex": self.settings.codex_launch_cmd,
            "opencode": self.settings.opencode_launch_cmd,
            "copilot": self.settings.copilot_launch_cmd,
            "cursor": self.settings.cursor_launch_cmd,
        }
        for alias, argv in launch_overrides.items():
            if argv:
                self.executors[f"harness:{alias}"] = make(list(argv))
        for name, argv in self.settings.harnesses.items():
            self.executors[f"harness:{name}"] = make(list(argv))

    def bind_agents(
        self,
        conduit: Conduit,
        agents: Mapping[str, str],
        *,
        origin: str = "--agent",
    ) -> Conduit:
        """Return the conduit to run with ``agents`` applied, or raise.

        The shape checks live in :mod:`flow_atelier.modules.binding`; this
        layer adds the one question only it can answer — whether an executor is
        actually registered for the named agent. Nothing is written and no
        agent is started: the installed recipe, the registry snapshot and the
        process's launcher table are all left untouched.

        :param conduit: the recipe as installed.
        :param agents: mapping of root or dotted task path to harness tool.
        :param origin: what asked for the change, for the diagnostics.
        :returns: the conduit to hand the engine.
        :raises BindingError: a selector or an agent name is unusable.
        """
        agents = normalize_bindings(agents, where=origin)
        bound = bind_agent_paths(
            conduit, agents, self.store.read_conduit, origin=origin
        )
        self._require_registered(agents, origin=origin)
        return bound

    def _require_registered(self, agents: Mapping[str, str], *, origin: str) -> None:
        """Raise unless an executor is registered for every named agent.

        Separate from the structural checks because the two answer different
        questions at different times: whether a selector still fits the recipe
        is about the recipe, while whether an agent exists is about this
        machine's configuration right now. A choice a later invocation replaces
        must not be held to the second one — that is what recovering from a
        removed agent means.

        :param agents: mapping of task name to harness tool.
        :param origin: what asked for these agents, for the diagnostics.
        :raises BindingError: nothing is registered for one of the agents.
        """
        for task, tool in agents.items():
            if resolve_executor(self.executors, tool) is None:
                raise BindingError(
                    f"{origin}: task {task!r} names unknown agent {tool!r} — run "
                    "'atelier list harnesses' to see the registered names, or "
                    "register your own with ATELIER_HARNESSES",
                    code=1,
                )

    def require_agent_ready(self, agents: Mapping[str, str]) -> None:
        """Check selected harness launchers before any task or flow starts."""
        for path, tool in agents.items():
            if "." not in path:
                continue  # existing root readiness gate owns its diagnostics
            executor = resolve_executor(self.executors, tool)
            if executor is None:
                continue  # _require_registered reports this with its own hint
            ok, reason = executor.is_available()
            if not ok:
                raise BindingError(
                    f"--agent {path}=...: {tool} cannot start: {reason}", code=1
                )

    def _inherit_agents(
        self,
        conduit: Conduit,
        flow_id: str,
        prior: Progress,
        agents: Mapping[str, str],
        *,
        executes: Container[str] | None = None,
    ) -> tuple[Conduit, dict[str, str]]:
        """Apply a prior run's saved agent choices, then this call's overrides.

        Saved choices are validated against the recipe *as it is now*: a
        selector the recipe no longer has, one that now names a ``tool:`` task,
        or a saved value that is not an agent name at all fails here rather
        than silently reverting that task to the recipe's agent.

        Whether an agent is still configured on this machine is asked of the
        work this call will actually hand to an executor. A saved choice this
        call replaces is on its way out, and one whose task is already done is
        only a record of who did it, so an agent uninstalled since that run must
        block neither the replacement nor the retained output — recovering from
        a removed agent is exactly what both are for.

        :param conduit: the recipe as installed right now.
        :param flow_id: the flow whose choices are being inherited.
        :param prior: that flow's saved progress.
        :param agents: this call's explicit overrides, already normalized,
            applied on top.
        :param executes: the task names this call will run, when it will not run
            them all. ``None`` — a re-run from the top — holds every effective
            agent to being registered.
        :returns: ``(conduit to run, effective assignment map)``.
        :raises BindingError: a saved or requested selection is unusable.
        """
        origin = f"flow {flow_id} saved agent choices"
        try:
            saved = normalize_bindings(dict(prior.task_agents), where=origin)
        except BindingError as exc:
            raise BindingError(
                f"{exc} — so they cannot be replayed; start a fresh run with "
                f"`atelier run {conduit.name} --agent ...`",
                code=1,
            ) from exc
        try:
            bound = bind_agent_paths(
                conduit, saved, self.store.read_conduit, origin=origin
            )
        except BindingError as exc:
            raise BindingError(
                f"{exc} — the recipe changed since that run, so its saved "
                f"choices cannot be replayed; start a fresh run with "
                f"`atelier run {conduit.name} --agent ...`",
                code=1,
            ) from exc
        bound = bind_agent_paths(
            bound, agents, self.store.read_conduit, origin="--agent"
        )
        effective = {**saved, **agents}
        # Availability is asked of what will actually run: an agent this call
        # replaces may have been uninstalled since, and that is precisely the
        # failure a replacement recovers from.
        self._require_registered(agents, origin="--agent")
        self._require_registered(
            {
                t: v for t, v in saved.items()
                if t not in agents and (
                    executes is None or t.split(".", 1)[0] in executes
                )
            },
            origin=f"{origin} (replace it with `--agent <task>=<agent>`)",
        )
        return bound, effective


    def _new_workspaces(
        self,
        conduit: Conduit,
        tasks: Sequence[str],
        flow_id: str,
        source_dir: Path | None,
        inputs: Mapping[str, Any],
        *,
        origin: str = "--worktree",
    ) -> Workspaces:
        """Create this run's separate checkouts, before any task starts.

        Everything that can refuse does so first — the selectors against the
        recipe, then the source repository — so a run that cannot be isolated
        costs no prompt and creates nothing. Once Git is asked to build them,
        whatever it built is kept even if a later one fails: a partly set up
        run is recoverable, deleted work is not.

        :param conduit: the recipe as this run will execute it.
        :param tasks: the top-level task names to isolate.
        :param flow_id: the id this run will use, naming the destination.
        :param source_dir: the directory to resolve the source checkout from,
            or ``None`` for the process's. A rerun inheriting a saved policy
            passes the repository the first run recorded, not the caller's.
        :param inputs: the inputs the run was given, before defaults.
        :param origin: what asked for the isolation, for the diagnostics.
        :returns: the :class:`Workspaces` to record on the run.
        :raises WorkspaceError: a selector, the source or a destination is
            unusable.
        :raises ValueError: the engine would refuse the run itself — an
            invalid recipe or a missing required input. Checked here too,
            because checkouts made for a run that never starts are recorded
            nowhere.
        """
        validate_conduit(conduit)
        missing = [
            k for k, spec in conduit.inputs.items()
            if spec.default is None and k not in inputs
        ]
        if missing:
            raise ValueError(f"missing required inputs: {missing}")
        check_selectors(conduit, tasks, origin=origin)
        source = resolve_source_repo(source_dir, origin=origin)
        return create_worktrees(source, self.store.workspace_dir(flow_id), tasks)

    def tool_readiness(
        self, conduit: Conduit, only: Container[str] | None = None
    ) -> list[str]:
        """Report why a conduit can't run, before any task executes.

        Walks ``conduit.tasks`` and, for each, confirms its tool is registered
        and its executor's :meth:`ExecutorBase.is_available` probe passes (e.g.
        a harness CLI present on PATH). This is the preflight gate used by
        ``atelier check`` and the top of ``atelier run`` so an unrunnable
        conduit fails in second one rather than mid-DAG. Structural validation
        stays in :func:`validate_conduit`; this layer owns runnability because
        it is the only one holding both the conduit and the executor registry.

        :param conduit: the loaded conduit to probe.
        :param only: when given, the task names to probe — a resume asks about
            the work still to do, because a task whose output is already saved
            is never handed to an executor again.
        :returns: ordered, de-duplicated problem messages; ``[]`` when ready.
        """
        problems: list[str] = []
        tools = [
            (f"task {task.name!r}", task.tool)
            for task in conduit.tasks
            if only is None or task.name in only
        ]
        if (
            conduit.interaction and conduit.interaction.supervisor is not None
            and {conduit.interaction.questions, conduit.interaction.permissions}
            & {"supervisor", "hybrid"}
        ):
            tools.append(("supervisor", conduit.interaction.supervisor.tool))
        for label, tool in tools:
            executor = resolve_executor(self.executors, tool)
            if executor is None:
                msg = f"{label}: no executor registered for tool {tool!r}"
            else:
                ok, reason = executor.is_available()
                if ok:
                    continue
                msg = f"{label} [{tool}]: {reason}"
            if msg not in problems:
                problems.append(msg)
        return problems

    def _require_ready(
        self, conduit: Conduit, only: Container[str] | None = None
    ) -> None:
        """Raise unless every tool this run will reach can actually be run.

        The gate ``atelier run`` applies to a fresh run, applied to the agents
        a resume or a re-run will actually use — including the ones inherited
        from the source flow. It runs before ``engine.run``, so a replacement
        that cannot start costs no earlier prompt, creates no new flow and
        leaves the source run's saved bytes alone.

        :param conduit: the conduit with this run's selections applied.
        :param only: the task names to probe; ``None`` probes them all.
        :raises BindingError: a tool this run would reach is unrunnable.
        """
        problems = self.tool_readiness(conduit, only=only)
        if problems:
            raise BindingError("cannot run: " + "; ".join(problems), code=1)

    async def run_conduit(
        self,
        name: str,
        inputs: dict[str, Any],
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | str | None = None,
        stoppable: bool = False,
        agents: Mapping[str, str] | None = None,
        worktrees: Sequence[str] | None = None,
    ) -> str:
        """Start a new flow for the named conduit.

        :param name: conduit name (must match a folder under ``conduits/``)
        :param inputs: conduit input map, keyed by input name
        :param on_task_event: optional callback invoked with a
            :class:`TaskEvent` after every task iteration finishes (success
            or failure). Exceptions raised by the callback are logged but
            do not affect the flow.
        :param on_flow_started: optional callback invoked once with the
            new flow id, before any task runs. Lets the caller record the
            id and surface it on failure as well as on success.
        :param on_task_starting: optional callback invoked with
            ``(task_name, tool)`` when a task begins its first iteration.
        :param show_steps: stream intermediate harness steps (thinking,
            tool calls, tool results) to the executor's prompt sink as
            they happen. Defaults to ``True``; the CLI exposes
            ``--hide-steps`` to opt out.
        :param working_dir: working directory for task execution. When
            ``None``, executors use the process cwd.
        :param stoppable: install a SIGTERM stop handler for this run (the
            ``atelier stop`` path); only the foreground CLI sets this.
        :param agents: per-task agent selections for this run only, as
            ``{task_name: "harness:<name>[:<model>[:<effort>]]"}``. Applied to
            the loaded conduit in memory and recorded on the flow's progress;
            the installed recipe is never rewritten.
        :param worktrees: top-level task names to run in their own detached
            Git worktree, cut from the working directory's checkout at its
            current HEAD before any task starts. The mapping is recorded on the
            flow so a later resume continues in the same directories. Omit it
            and every task shares one working directory, as before.
        :returns: the newly created flow id
        :raises BindingError: a selection names no reachable harness task of
            the conduit, or an agent nothing is registered for
        :raises WorkspaceError: a worktree selection, the source checkout or a
            destination directory is unusable
        """
        wd = Path(working_dir) if working_dir is not None else None
        # An isolated run pins where it was started, not just which commit: the
        # source repository is resolved from this directory, and a later
        # `--again` has to cut its fresh checkouts from the same code even when
        # it is invoked from somewhere else entirely. Left unset, a rerun would
        # silently take whatever repository the caller happened to stand in.
        if worktrees and wd is None:
            wd = Path.cwd()
        conduit = self.store.read_conduit(name)
        agents = normalize_bindings(dict(agents or {}))
        conduit = self.bind_agents(conduit, agents)
        self.require_agent_ready(agents)
        # The destination is named after the flow, so the id has to exist
        # before the checkouts do. The engine takes it as given.
        flow_id = new_flow_id(name) if worktrees else None
        spaces = (
            self._new_workspaces(conduit, list(worktrees), flow_id, wd, inputs)
            if worktrees
            else None
        )
        return await self.engine.run(
            conduit,
            inputs,
            on_task_event=on_task_event,
            on_flow_started=on_flow_started,
            on_task_starting=on_task_starting,
            show_steps=show_steps,
            working_dir=wd,
            flow_id=flow_id,
            stoppable=stoppable,
            task_agents=agents,
            workspaces=spaces,
        )

    async def resume_flow(
        self,
        flow_id: str,
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | str | None = None,
        stoppable: bool = False,
        agents: Mapping[str, str] | None = None,
    ) -> str:
        """Resume a failed or crashed flow, skipping already-completed tasks.

        A flow whose process died (crash, kill, power loss) is left with
        status ``running``, so that status is resumable too — matching how
        nested flows are recovered. As a guard against double-running, resume
        refuses when the original runner pid is *provably* still alive on this
        host (see :func:`is_runner_alive`). The honest limitation remains: a
        runner on another host can't be probed, so a cross-machine
        still-running flow could still be double-run.

        Resume is at-least-once: an iteration's log entry is written before its
        completion/output is persisted, so an iteration that finished but whose
        completion was killed before being saved will execute again on resume.
        This is mostly harmless, but for a paid AI-agent task it can re-spend
        tokens on work that was already done. Recovering loop context also
        re-reads and re-parses the full log file once per resume, a cost that
        grows with long, repeatedly-resumed runs (folded into the retention/
        pruning work rather than fixed here).

        :param flow_id: flow id of the prior failed/crashed run to resume
        :param on_task_event: optional task-event callback forwarded to the engine
        :param on_flow_started: optional flow-started callback
        :param on_task_starting: optional task-starting callback
        :param show_steps: stream intermediate harness steps
        :param working_dir: working directory for task execution
        :param stoppable: install a SIGTERM stop handler for this run (the
            ``atelier stop`` path); only the foreground CLI sets this.
        :param agents: replacement agents for tasks this resume has still to
            run. Omit it and the flow's saved choices are reused as they are.
            Only a pending or failed task may be re-pointed, and every check —
            including whether the agents this resume would use can actually be
            started — happens before any saved byte is touched.
        :returns: the flow id (same as input)
        :raises WorkspaceError: a checkout this resume has to continue in is
            missing, is no longer a worktree, or belongs to another repository
        :raises ValueError: if the flow is not in failed or running status
        :raises BindingError: a saved or requested selection is unusable, or a
            task whose assignment is already fixed was named
        """
        prior = self.store.read_progress(flow_id)
        if prior.status not in (FlowStatus.failed, FlowStatus.running):
            raise ValueError(
                f"can only resume failed or crashed flows, got {prior.status.value}"
            )
        if is_runner_alive(prior):
            raise ValueError(
                f"flow {flow_id} runner pid {prior.runner_pid} is still alive on "
                "this host; refusing to resume to avoid a double-run"
            )
        conduit_name, _, _ = parse_flow_id(flow_id)
        conduit = self.store.read_conduit(conduit_name)
        requested = normalize_bindings(dict(agents or {}))
        # Every gate below asks about the work this resume will hand to an
        # executor. A completed task is not re-run: its agent is a record of who
        # produced the saved output, not a choice this run has to be able to make
        # again.
        unfinished = {
            t.name for t in conduit.tasks
            if prior.tasks.get(t.name) is None
            or prior.tasks[t.name].status != TaskStatus.completed
        }
        partial = self._partly_looped(flow_id, conduit, unfinished)
        if requested:
            check_replaceable(
                prior, {k: v for k, v in requested.items() if "." not in k},
                partial=partial,
            )
            for path in requested:
                if "." not in path:
                    continue
                self._check_nested_replacement(
                    conduit, flow_id, prior, path, requested[path], partial
                )
        conduit, effective = self._inherit_agents(
            conduit, flow_id, prior, requested, executes=unfinished
        )
        by_name = {t.name: t.tool for t in conduit.tasks}
        for tname, ran_on in partial.items():
            if by_name.get(tname, ran_on) == ran_on:
                continue
            raise BindingError(
                f"task {tname!r} recorded iterations on {ran_on} and this run "
                f"would continue it on {by_name[tname]}; its "
                "'{{loop.history}}' would then mix two agents and be "
                "attributed to one — start a fresh run with `atelier run "
                f"--again {flow_id}`",
                code=1,
            )
        if effective:
            self._require_ready(conduit, only=unfinished)
            self.require_agent_ready({
                k: v for k, v in effective.items()
                if k.split(".", 1)[0] in unfinished
            })
        # The checkouts are the run's, not this invocation's: a resume
        # continues in the exact directories the first attempt recorded, with
        # whatever a failed worker left in them. Drift in the recipe is caught
        # against the recipe as it is now, before a single agent starts.
        owned_root = self.store.workspace_dir(flow_id)
        if prior.workspaces is None:
            check_unrecorded(owned_root)
        else:
            check_selectors(
                conduit,
                prior.workspaces.paths,
                origin=f"flow {flow_id} saved worktrees",
            )
            verify_worktrees(prior.workspaces, unfinished, owned_root=owned_root)
        inputs = self.store.read_input(flow_id)
        if working_dir is None and prior.run_path:
            working_dir = prior.run_path
        wd = Path(working_dir) if working_dir is not None else None
        return await self.engine.run(
            conduit,
            inputs,
            on_task_event=on_task_event,
            on_flow_started=on_flow_started,
            on_task_starting=on_task_starting,
            show_steps=show_steps,
            working_dir=wd,
            resume_from=flow_id,
            stoppable=stoppable,
            task_agents=effective,
            workspaces=prior.workspaces,
        )

    def _check_nested_replacement(
        self, conduit: Conduit, flow_id: str, progress: Progress,
        path: str, tool: str, partial: Mapping[str, str],
    ) -> None:
        """Apply the top-level replacement safety rule at each call in a path."""
        parts = path.split(".")
        for call in parts[:-1]:
            check_replaceable(progress, {call: tool}, partial=partial)
            task = next((t for t in conduit.tasks if t.name == call), None)
            if task is None or task.tool != "tool:conduit" or "{{" in task.task:
                return  # bind_agent_paths supplies the selector diagnostic
            child_id = self.engine._find_child_to_resume(
                flow_id, task.task.strip(), call
            )
            if child_id is None:
                return
            flow_id = child_id
            conduit = self.store.read_conduit(task.task.strip())
            progress = self.store.read_progress(flow_id)
            partial = self._partly_looped(flow_id, conduit, set(progress.tasks))
        check_replaceable(progress, {parts[-1]: tool}, partial=partial)

    def _partly_looped(
        self, flow_id: str, conduit: Conduit, unfinished: Container[str]
    ) -> dict[str, str]:
        """Return the looping tasks of ``flow_id`` left half-run.

        Their ``{{loop.history}}`` was produced by the tool they started with,
        so a later resume may neither hand the rest of the loop to another one
        nor let the recipe's own edit do it silently. Read from the log the
        engine itself replays on resume.

        A loop that ran every iteration is not half-run: the resume replays its
        saved output and starts no iteration, so there is no remaining history
        for a second agent to join. Only a loop that still owes iterations is
        reported here.

        :param flow_id: the flow whose log to read.
        :param conduit: the recipe, for each task's ``repeat``.
        :param unfinished: the task names this resume will execute.
        :returns: mapping of looping task name to the tool its recorded
            iterations ran on, for those with a successful iteration still to
            be continued.
        """
        looping = {
            t.name for t in conduit.tasks if t.repeat > 1 and t.name in unfinished
        }
        if not looping:
            return {}
        return {
            entry.task: entry.tool
            for entry in self.store.read_logs(flow_id)
            if entry.task in looping and entry.exit_code == 0
        }

    async def rerun_flow(
        self,
        flow_id: str,
        overrides: dict[str, Any] | None = None,
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | str | None = None,
        stoppable: bool = False,
        agents: Mapping[str, str] | None = None,
        worktrees: Sequence[str] | None = None,
    ) -> str:
        """Start a brand-new flow of a past run's conduit, reusing its inputs.

        Unlike :meth:`resume_flow`, this does not continue the old run: it
        allocates a fresh flow id and re-executes the whole conduit from the
        top. There is no status gate, so a ``completed`` flow can be repeated.
        The source flow's persisted ``input.yaml`` is reused verbatim; keys in
        ``overrides`` win, letting the caller vary individual inputs while
        keeping the rest. The working directory is not an input; it comes from
        the ``working_dir`` argument, falling back to the source flow's recorded
        ``run_path`` when omitted.

        :param flow_id: flow id of the prior run whose inputs to reuse
        :param overrides: per-key input overrides applied on top of the stored
            inputs; defaults to ``{}``
        :param on_task_event: optional task-event callback forwarded to the engine
        :param on_flow_started: optional flow-started callback
        :param on_task_starting: optional task-starting callback
        :param show_steps: stream intermediate harness steps
        :param working_dir: working directory for task execution; when ``None``,
            falls back to the source flow's recorded ``run_path``
        :param stoppable: install a SIGTERM stop handler for this run (the
            ``atelier stop`` path); only the foreground CLI sets this.
        :param agents: per-task agent selections applied on top of the source
            run's saved choices. The new flow records its own assignment map;
            the source run is not modified. An agent that cannot be started is
            refused before the new flow exists, so no earlier task is prompted
            for a run that was going to fail anyway.
        :param worktrees: extra top-level task names to isolate, on top of the
            ones the source run isolated. The policy is inherited; the
            directories are not — this is a new run, so it gets new checkouts
            at the source repository's *current* HEAD, and the old run's
            directories are left exactly as its agents left them. The
            repository is the one the source run recorded, not the caller's, so
            a rerun started elsewhere still works on the same code; if that
            repository is gone the rerun fails before any prompt. An explicit
            ``working_dir`` overrides it.
        :returns: the newly created flow id (distinct from ``flow_id``)
        :raises FileNotFoundError: if the source flow or its conduit is gone
        :raises BindingError: a saved or requested selection is unusable
        :raises WorkspaceError: an inherited or requested worktree selection,
            the source checkout or a destination directory is unusable
        """
        conduit_name, _, _ = parse_flow_id(flow_id)
        conduit = self.store.read_conduit(conduit_name)
        inputs = {**self.store.read_input(flow_id), **(overrides or {})}
        prior = self.store.read_progress(flow_id)
        conduit, effective = self._inherit_agents(
            conduit, flow_id, prior, normalize_bindings(dict(agents or {}))
        )
        if effective:
            self._require_ready(conduit)
            self.require_agent_ready(effective)
        chosen_dir = working_dir is not None
        if working_dir is None and prior.run_path:
            working_dir = prior.run_path
        wd = Path(working_dir) if working_dir is not None else None
        inherited: list[str] = []
        if prior.workspaces is None:
            check_unrecorded(self.store.workspace_dir(flow_id))
        else:
            # A damaged record is not a policy to inherit: read as-is it would
            # silently drop the isolation and put every writer back in one
            # directory.
            check_record(prior.workspaces, self.store.workspace_dir(flow_id))
            inherited = list(prior.workspaces.paths)
        wanted = inherited + [t for t in (worktrees or []) if t not in inherited]
        # The same brief on the same code: an inherited policy keeps the
        # repository the first run recorded, so running `--again` from another
        # directory repeats the work rather than pointing the agents at
        # whatever happens to be checked out there. An explicit working
        # directory from the caller still wins — that is a deliberate choice.
        source_dir = (
            Path(prior.workspaces.source)
            if inherited and not chosen_dir
            else wd
        )
        new_id = new_flow_id(conduit_name) if wanted else None
        spaces = (
            self._new_workspaces(
                conduit,
                wanted,
                new_id,
                source_dir,
                inputs,
                origin=(
                    "--worktree" if not inherited
                    else f"flow {flow_id} saved worktrees"
                ),
            )
            if wanted
            else None
        )
        return await self.engine.run(
            conduit,
            inputs,
            on_task_event=on_task_event,
            on_flow_started=on_flow_started,
            on_task_starting=on_task_starting,
            show_steps=show_steps,
            working_dir=wd,
            flow_id=new_id,
            stoppable=stoppable,
            task_agents=effective,
            workspaces=spaces,
        )

    def get_status(self, flow_id: str) -> Progress:
        """Return the latest :class:`Progress` snapshot for ``flow_id``.

        :param flow_id: flow identifier
        :returns: current progress snapshot
        """
        return self.store.read_progress(flow_id)

    def get_outputs(self, flow_id: str) -> dict[str, Any]:
        """Return the per-task results saved to ``outputs.yaml`` for ``flow_id``.

        :param flow_id: flow identifier
        :returns: mapping of task name to output value; ``{}`` if no
            ``outputs.yaml`` has been written yet (flow still running or it
            failed before any task completed)
        """
        return self.store.read_outputs(flow_id)

    def list_conduits(self) -> list[str]:
        """List all available conduit names.

        :returns: sorted list of conduit names
        """
        return self.store.list_conduits()

    def list_flows(self, conduit_name: str | None = None) -> list[str]:
        """List flow ids, optionally filtered by conduit.

        :param conduit_name: restrict to flows of this conduit
        :returns: sorted list of flow ids
        """
        return self.store.list_flows(conduit_name)

    # ------------------------------------------------------------------ CRUD

    def create_conduit(self, payload: CreateConduitInput) -> Conduit:
        """Persist a new conduit; raise if one with that name already exists.

        :param payload: validated :class:`CreateConduitInput`
        :returns: the persisted :class:`Conduit`
        :raises FileExistsError: if a conduit with that name already exists
            in the project or global store
        """
        try:
            self.store.conduit_source(payload.name)
            raise FileExistsError(f"conduit already exists: {payload.name}")
        except FileNotFoundError:
            pass
        conduit = Conduit.model_validate(payload.model_dump(exclude={"run_path"}))
        self.store.write_conduit(conduit)
        return conduit

    def update_conduit(
        self, name: str, payload: UpdateConduitInput
    ) -> Conduit:
        """Apply a partial update to an existing conduit.

        :param name: conduit to modify
        :param payload: subset of fields to overwrite
        :returns: the updated :class:`Conduit`
        :raises FileNotFoundError: if the conduit doesn't exist
        :raises FileExistsError: if the update renames to a name already taken
        """
        existing = self.store.read_conduit(name)
        merged = existing.model_dump()
        if "interaction" in payload.model_fields_set:
            merged["interaction"] = payload.interaction
        for key, value in payload.model_dump(exclude_none=True).items():
            merged[key] = value
        merged["name"] = merged.get("name") or name
        updated = Conduit.model_validate(merged)
        if updated.name != name:
            # Rename: refuse to clobber an existing conduit at the target name.
            try:
                self.store.conduit_source(updated.name)
                raise FileExistsError(f"conduit already exists: {updated.name}")
            except FileNotFoundError:
                pass
        self.store.write_conduit(updated)
        if updated.name != name:
            # Rename: drop the old folder.
            self.store.delete_conduit(name)
        return updated

    def delete_conduit(self, name: str) -> bool:
        """Remove a project-level conduit.

        :param name: conduit name
        :returns: True if it existed and was deleted, False otherwise
        """
        return self.store.delete_conduit(name)

    def delete_flow(self, flow_id: str) -> bool:
        """Remove a flow directory and its nested child subtree.

        :param flow_id: flow identifier
        :returns: True if it existed and was deleted, False otherwise
        """
        return self.store.delete_flow(flow_id)

    # ------------------------------------------------------------------ packages

    def _lockfile_path(self) -> Path:
        """Return the install lockfile path under the global atelier dir."""
        return self.settings.global_atelier_dir / "installed.json"

    def install_package(
        self,
        source: str,
        *,
        ref: str | None = None,
        project: bool = False,
        force: bool = False,
    ) -> InstallReport:
        """Fetch a package and install its conduits, then lock it.

        :param source: git URL, ``owner/repo``, or local path.
        :param ref: optional git ref to check out.
        :param project: install conduits into the project store instead of global.
        :param force: overwrite colliding conduits.
        :returns: an :class:`InstallReport` of what was installed/skipped.
        """
        src = resolve_source(source)
        cache_root = self.settings.global_atelier_dir / "cache"
        repo_dir = fetch_source(src, cache_root, ref)
        manifest = read_package(repo_dir)
        conduit_base = (
            self.settings.atelier_dir if project else self.settings.global_atelier_dir
        )
        conduit_root = conduit_base / "conduits"
        report = install_package(
            repo_dir, manifest,
            conduit_root=conduit_root,
            scope="project" if project else "global", force=force,
        )
        write_lockfile(
            self._lockfile_path(),
            manifest.name,
            {
                "source": src.location,
                "ref": ref or "",
                "conduits": report.conduits_installed,
                "scope": report.scope,
            },
        )
        return report

    def update_package(self, name: str, *, force: bool = False) -> InstallReport:
        """Re-fetch and re-install a package from its recorded source.

        Reuses the recorded source/ref/scope so a user's hand-made schedule
        survives (D7). Without ``--force``, items already present skip-and-warn
        but stay package-owned (their lockfile ownership is preserved across the
        update).

        :param name: installed package name (lockfile key).
        :param force: overwrite existing conduits.
        :raises PackageError: if no package by that name is installed.
        """
        entry = read_lockfile(self._lockfile_path()).get(name)
        if entry is None:
            raise PackageError(
                f"package not installed: {name} (install it with `atelier add`)"
            )
        report = self.install_package(
            entry["source"],
            ref=entry.get("ref") or None,
            project=entry.get("scope") == "project",
            force=force,
        )
        # install_package wrote the lockfile with only what it (re)installed;
        # union with prior ownership so skipped-but-owned items aren't dropped.
        merged_conduits = sorted(
            set(entry.get("conduits", [])) | set(report.conduits_installed)
        )
        new_entry = read_lockfile(self._lockfile_path()).get(name, {})
        new_entry["conduits"] = merged_conduits
        write_lockfile(self._lockfile_path(), name, new_entry)
        return report

    def remove_package(self, name: str) -> RemoveReport:
        """Delete exactly the conduit dirs a package installed.

        Only lockfile-owned items are removed: collision-skipped conduits, user
        data, and user schedules are left intact (D6, D7).

        :param name: installed package name (lockfile key).
        :raises PackageError: if no package by that name is installed.
        """
        entry = read_lockfile(self._lockfile_path()).get(name)
        if entry is None:
            raise PackageError(
                f"package not installed: {name} (see installed packages with `atelier add`)"
            )
        report = RemoveReport(name=name)
        global_scope = entry.get("scope", "global") == "global"
        for conduit in entry.get("conduits", []):
            removed = (
                self.store.delete_conduit_global(conduit)
                if global_scope
                else self.store.delete_conduit(conduit)
            )
            if removed:
                report.conduits_removed.append(conduit)
        delete_lockfile_entry(self._lockfile_path(), name)
        return report

    async def run_single_task(self, payload: RunTaskInput) -> RunTaskOutput:
        """Run an ad-hoc one-task conduit and return the resulting logs.

        :param payload: validated :class:`RunTaskInput`
        :returns: :class:`RunTaskOutput` carrying the flow id and logs
        :raises ValueError: if ``name`` or ``tool`` is well-formed JSON but
            invalid as a task definition (e.g. a hyphenated name or an unknown
            tool), so the route can map it to a 400 instead of a 500
        """
        try:
            conduit = Conduit.model_validate(
                {
                    "name": f"task__{payload.name}",
                    "description": payload.description or payload.name,
                    "tasks": [
                        {
                            "name": payload.name,
                            "description": payload.description or payload.name,
                            "task": payload.task,
                            "tool": payload.tool,
                            "depends_on": [],
                        }
                    ],
                }
            )
        except ValidationError as e:
            raise ValueError(f"invalid task definition: {e}") from e
        captured: dict[str, str | None] = {"id": None}

        def _on_started(fid: str) -> None:
            """Capture the flow id emitted by the engine before tasks run.

            :param fid: flow id assigned by the engine.
            """
            # The engine also reports each nested tool:conduit run; keep the first.
            if captured["id"] is None:
                captured["id"] = fid

        # A failing task is a result, not a transport error — the run happened,
        # it just didn't succeed — so it stays a 200 and reports itself in the
        # body. Swallowing it silently is what made a failure indistinguishable
        # from a task that simply printed nothing.
        error = ""
        try:
            flow_id = await self.engine.run(
                conduit,
                {},
                on_flow_started=_on_started,
                working_dir=Path(payload.run_path) if payload.run_path else None,
            )
        except Exception as exc:  # noqa: BLE001
            flow_id = captured["id"] or ""
            error = f"{type(exc).__name__}: {exc}"
        logs = self.store.read_logs(flow_id) if flow_id else []
        return RunTaskOutput(
            flow_id=flow_id, logs=logs, success=not error, error=error
        )

    # ------------------------------------------------------------------ schedules

    def list_schedules(self) -> list[ScheduledJob]:
        """Return every schedule persisted by this Atelier."""
        return self.schedule_store.list()

    def create_schedule(self, payload: CreateScheduleInput) -> ScheduledJob:
        """Persist a new schedule and return it.

        Validates that ``conduit_name`` resolves to a known conduit (in the
        same store the fire will use), that every input the schedule supplies
        is one the conduit declares or references, and that the schedule
        supplies every required (default-less) input the conduit declares, so
        a typo or a missing input fails loudly here instead of silently at
        fire time via a swallowed exception. Unknown keys are checked before
        missing ones so a typo of a required key is reported as the typo.

        :param payload: validated :class:`CreateScheduleInput`
        :returns: the new :class:`ScheduledJob`
        :raises ValueError: if ``conduit_name`` is not a known conduit, the
            schedule supplies an input the conduit cannot use, or the
            schedule omits a required (default-less) conduit input
        """
        if payload.conduit_name not in self.store.list_conduits():
            raise ValueError(f"unknown conduit: {payload.conduit_name!r}")
        conduit = self.store.read_conduit(payload.conduit_name)
        check_unknown_inputs(
            conduit, payload.inputs, subject=f"schedule for {payload.conduit_name!r}"
        )
        required = {
            key for key, spec in conduit.inputs.items() if spec.default is None
        }
        missing = required - set(payload.inputs)
        if missing:
            raise ValueError(
                f"schedule for {payload.conduit_name!r} is missing required "
                f"inputs: {sorted(missing)}"
            )
        return self.schedule_store.create(payload)

    def delete_schedule(self, schedule_id: str) -> ScheduledJob:
        """Delete a schedule by id (hard delete; the YAML file is removed).

        :param schedule_id: schedule identifier
        :returns: the :class:`ScheduledJob` as it was just before removal
        :raises KeyError: if the schedule doesn't exist
        """
        return self.schedule_store.delete(schedule_id)

    # ------------------------------------------------------------------ history

    def list_prior_flows(self) -> list[PriorFlow]:
        """Return :class:`PriorFlow` summaries for every flow on disk."""
        out: list[PriorFlow] = []
        for flow_id in self.store.list_flows():
            try:
                conduit_name, _, _ = parse_flow_id(flow_id)
            except ValueError:
                continue
            try:
                progress = self.store.read_progress(flow_id)
                status = progress.status.value
                started_at = progress.started_at
                finished_at = progress.finished_at
            except (FileNotFoundError, ValueError):
                status = "unknown"
                started_at = None
                finished_at = None
            out.append(
                PriorFlow(
                    flow_id=flow_id,
                    conduit_name=conduit_name,
                    started_at=started_at,
                    finished_at=finished_at,
                    status=status,
                )
            )
        return out

    def get_flow_logs(self, flow_id: str) -> list[LogEntry]:
        """Return the log entries for ``flow_id`` including all descendant logs.

        Descendant flow entries are tagged with ``extra["flow_id"]`` so callers
        can distinguish their origin. Aggregation recurses depth-first, so a
        ``tool:conduit`` whose child itself nests a conduit contributes its
        grandchildren's logs too.

        :param flow_id: flow identifier
        :returns: list of :class:`LogEntry` (empty if the file is empty)
        :raises FileNotFoundError: if no flow with that id exists
        """
        # ``_flow_dir`` raises FileNotFoundError when the id is unknown.
        self.store._flow_dir(flow_id)
        logs = self.store.read_logs(flow_id)
        for child_id in self.store.list_child_flows(flow_id):
            logs.extend(self._descendant_logs(child_id))
        return logs

    def _descendant_logs(self, flow_id: str) -> list[LogEntry]:
        """Return ``flow_id``'s logs plus all descendants', tagged by origin.

        :param flow_id: flow identifier whose own and descendant logs to gather
        :returns: log entries tagged with ``extra["flow_id"]`` of their flow
        """
        entries = [
            entry.model_copy(
                update={"extra": {**(entry.extra or {}), "flow_id": flow_id}}
            )
            for entry in self.store.read_logs(flow_id)
        ]
        for child_id in self.store.list_child_flows(flow_id):
            entries.extend(self._descendant_logs(child_id))
        return entries

    def get_flow_view(self, flow_id: str) -> FlowView:
        """Return every task of ``flow_id`` with its dependencies and progress.

        Dependencies come from the conduit's current definition, so a conduit
        edited since the run shows its new shape around the tasks that ran.

        :param flow_id: flow identifier
        :returns: the run page's map
        :raises FileNotFoundError: if no flow with that id exists
        """
        # A sub-run lives at <parent>/flows/<id>; a top-level run at <base>/flows/<id>.
        holder = self.store._flow_dir(flow_id).parent.parent
        parent_flow_id = None if holder == self.store.base_dir else holder.name
        try:
            conduit_name, _, _ = parse_flow_id(flow_id)
        except ValueError:
            conduit_name = ""
        try:
            conduit = self.store.read_conduit(conduit_name) if conduit_name else None
        except (FileNotFoundError, ValueError):
            conduit = None
        return build_flow_view(
            flow_id, conduit_name, self.store.read_progress(flow_id), conduit, parent_flow_id
        )

    def flow_files_signature(self, flow_id: str) -> tuple[tuple[int, int] | None, ...]:
        """Return the mtime and size of the files a flow writes as it runs.

        Cheap enough to call several times a second: a watcher compares it
        between looks and reads the files only when it changed.

        :param flow_id: flow identifier
        :returns: ``(mtime_ns, size)`` for ``progress.json``, ``logs.jsonl``,
            ``steps.jsonl`` and the ``flows/`` dir that holds its sub-runs, in
            that order; ``None`` for one not written yet
        :raises FileNotFoundError: if no flow with that id exists
        """
        flow_dir = self.store._flow_dir(flow_id)
        found: list[tuple[int, int] | None] = []
        for name in ("progress.json", "logs.jsonl", "steps.jsonl", "flows"):
            try:
                st = (flow_dir / name).stat()
            except FileNotFoundError:
                found.append(None)
            else:
                found.append((st.st_mtime_ns, st.st_size))
        return tuple(found)

    def task_sub_runs(self, flow_id: str, task: str) -> list[tuple[str, str]]:
        """Return the sub-runs ``task`` started in ``flow_id``, as ``(started_at, flow_id)``.

        :param flow_id: flow identifier
        :param task: the ``tool:conduit`` task's name
        :returns: one pair per sub-run; unreadable ones are left out
        """
        found: list[tuple[str, str]] = []
        for child in self.store.list_child_flows(flow_id):
            try:
                p = self.store.read_progress(child)
            except (FileNotFoundError, ValueError):
                continue
            if p.invoking_task == task and p.started_at:
                found.append((p.started_at, child))
        return found

    def get_task_log(self, flow_id: str, task: str) -> TaskLogView:
        """Return one task's log in ``flow_id``: its rounds and every action.

        :param flow_id: flow identifier
        :param task: task name
        :returns: the task's log
        :raises FileNotFoundError: if no flow with that id exists
        :raises KeyError: if the flow has no task with that name
        """
        view = self.get_flow_view(flow_id)
        known = next((t for t in view.tasks if t.name == task), None)
        if known is None:
            raise KeyError(task)
        entries = [e for e in self.store.read_logs(flow_id) if e.task == task]
        steps, _ = self.store.read_steps(flow_id)
        tool = known.tool or (entries[0].tool if entries else "")
        return build_task_log(
            task,
            tool,
            self.store.read_progress(flow_id).tasks.get(task),
            entries,
            [s for s in steps if s.task == task],
            self.task_sub_runs(flow_id, task) if tool == "tool:conduit" else None,
        )

    def _known_run_paths(self) -> set[Path]:
        """Return the resolved ``run_path`` of every known flow.

        :returns: set of resolved run-path directories recorded on flow progress.
        """
        known: set[Path] = set()
        for flow_id in self.store.list_flows():
            try:
                rp = self.store.read_progress(flow_id).run_path
            except (FileNotFoundError, ValueError):
                continue
            if rp:
                known.add(Path(rp).resolve())
        return known

    def open_conduit_path(self, run_path: str) -> bool:
        """Reveal ``run_path`` in the host's file explorer.

        Only paths recorded as a flow's ``run_path`` are opened: the OS opener
        can launch arbitrary apps/documents, so an unconstrained caller (the
        default deployment has no token) must not be able to point it anywhere.

        :param run_path: absolute path to open
        :returns: True if the platform opener was launched, False otherwise
        """
        if Path(run_path).resolve() not in self._known_run_paths():
            logger.warning("open_conduit_path refused unknown path: %s", run_path)
            return False
        target = str(Path(run_path))
        cmd: list[str]
        if sys.platform == "darwin":
            cmd = ["open", target]
        elif sys.platform == "win32":
            cmd = ["explorer", target]
        else:
            cmd = ["xdg-open", target]
        try:
            subprocess.Popen(cmd)
        except (FileNotFoundError, OSError) as e:
            logger.warning("open_conduit_path failed: %s", e)
            return False
        return True
