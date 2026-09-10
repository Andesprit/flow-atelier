"""Facade: wires store + executors + engine and exposes the public API."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from flow_atelier.core.settings import AtelierSettings
from flow_atelier.modules.engine import (
    Engine,
    FlowStartedCallback,
    TaskEventCallback,
    TaskStartingCallback,
)
from flow_atelier.modules.liveness import is_runner_alive
from flow_atelier.schemas.api import (
    CreateConduitInput,
    CreateScheduleInput,
    PriorFlow,
    RunTaskInput,
    RunTaskOutput,
    ScheduledJob,
    UpdateConduitInput,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.log import LogEntry
from flow_atelier.schemas.progress import FlowStatus, Progress
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

    def tool_readiness(self, conduit: Conduit) -> list[str]:
        """Report why a conduit can't run, before any task executes.

        Walks ``conduit.tasks`` and, for each, confirms its tool is registered
        and its executor's :meth:`ExecutorBase.is_available` probe passes (e.g.
        a harness CLI present on PATH). This is the preflight gate used by
        ``atelier check`` and the top of ``atelier run`` so an unrunnable
        conduit fails in second one rather than mid-DAG. Structural validation
        stays in :func:`validate_conduit`; this layer owns runnability because
        it is the only one holding both the conduit and the executor registry.

        :param conduit: the loaded conduit to probe.
        :returns: ordered, de-duplicated problem messages; ``[]`` when ready.
        """
        problems: list[str] = []
        tools = [(f"task {task.name!r}", task.tool) for task in conduit.tasks]
        if (
            conduit.interaction and conduit.interaction.supervisor is not None
            and {conduit.interaction.questions, conduit.interaction.permissions}
            & {"supervisor", "hybrid"}
        ):
            tools.append(("supervisor", conduit.interaction.supervisor.tool))
        for label, tool in tools:
            executor = self.executors.get(tool)
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
        :returns: the newly created flow id
        """
        wd = Path(working_dir) if working_dir is not None else None
        conduit = self.store.read_conduit(name)
        return await self.engine.run(
            conduit,
            inputs,
            on_task_event=on_task_event,
            on_flow_started=on_flow_started,
            on_task_starting=on_task_starting,
            show_steps=show_steps,
            working_dir=wd,
            stoppable=stoppable,
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
        :returns: the flow id (same as input)
        :raises ValueError: if the flow is not in failed or running status
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
        )

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
        :returns: the newly created flow id (distinct from ``flow_id``)
        :raises FileNotFoundError: if the source flow or its conduit is gone
        """
        conduit_name, _, _ = parse_flow_id(flow_id)
        conduit = self.store.read_conduit(conduit_name)
        inputs = {**self.store.read_input(flow_id), **(overrides or {})}
        if working_dir is None:
            stored_run_path = self.store.read_progress(flow_id).run_path
            if stored_run_path:
                working_dir = stored_run_path
        wd = Path(working_dir) if working_dir is not None else None
        return await self.engine.run(
            conduit,
            inputs,
            on_task_event=on_task_event,
            on_flow_started=on_flow_started,
            on_task_starting=on_task_starting,
            show_steps=show_steps,
            working_dir=wd,
            stoppable=stoppable,
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
        conduit = Conduit.model_validate(payload.model_dump())
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
        same store the fire will use) and that the schedule supplies every
        required (default-less) input the conduit declares, so a typo or a
        missing input fails loudly here instead of silently at fire time via
        a swallowed exception.

        :param payload: validated :class:`CreateScheduleInput`
        :returns: the new :class:`ScheduledJob`
        :raises ValueError: if ``conduit_name`` is not a known conduit, or the
            schedule omits a required (default-less) conduit input
        """
        if payload.conduit_name not in self.store.list_conduits():
            raise ValueError(f"unknown conduit: {payload.conduit_name!r}")
        conduit = self.store.read_conduit(payload.conduit_name)
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
