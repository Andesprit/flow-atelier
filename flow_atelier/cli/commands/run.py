"""`atelier run` command, and the terminal driver every flow-running command shares."""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import sys
import time
from collections.abc import Awaitable, Callable

import typer
import yaml
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import (
    _exit_unknown_conduit,
    _parse_input_files,
    _parse_inputs,
    _resolve_flow_id,
    console,
    mark_activity,
    seconds_since_activity,
)
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import (
    _render_orchestration_msg,
    format_conduit_error,
    render_heartbeat,
    render_run_footer,
    render_task_event,
    render_task_start,
)
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import accepted_input_keys
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.log import TaskEvent
from flow_atelier.schemas.progress import TaskStatus

# How long the run stream must stay silent before the heartbeat speaks up.
HEARTBEAT_SECONDS = 30.0

# How often the heartbeat wakes to check for silence.
_HEARTBEAT_TICK_SECONDS = 1.0


class _RunningTasks:
    """Tracks which tasks are in flight and since when, for the heartbeat.

    ``on_task_starting`` fires once per task while task events fire once
    per iteration, so a repeating task stays tracked until its final
    iteration lands or it reaches a non-completed disposition.
    """

    def __init__(self) -> None:
        """Initialize with no tasks in flight."""
        self._started: dict[str, float] = {}
        self.count = 0

    def start(self, task_name: str) -> int:
        """Record ``task_name`` as running and return its 1-based position.

        :param task_name: name of the task entering the running state.
        :returns: how many tasks have started so far this run.
        """
        self._started[task_name] = time.monotonic()
        self.count += 1
        return self.count

    def finish(self, event: TaskEvent) -> None:
        """Stop tracking ``event.task`` once it has no further iterations.

        :param event: the task event just emitted.
        """
        still_looping = (
            event.status == TaskStatus.completed and event.iteration < event.of
        )
        if not still_looping:
            self._started.pop(event.task, None)

    def elapsed(self) -> dict[str, float]:
        """Return seconds elapsed for each in-flight task.

        :returns: mapping of task name to seconds since it started.
        """
        now = time.monotonic()
        return {name: now - started for name, started in self._started.items()}


async def _with_heartbeat(coro, running: _RunningTasks):
    """Await ``coro`` while emitting a periodic "still working" line.

    The heartbeat only prints during genuine silence, so an actively
    streaming run never sees it. Its job is the quiet stretches — an
    ``npx`` cold start, a multi-minute tool call, a ``tool:bash`` task
    that emits no steps — where the terminal is otherwise indistinguishable
    from a hang.

    :param coro: the engine coroutine to run.
    :param running: tracker naming the in-flight tasks.
    :returns: whatever ``coro`` returns.
    """

    async def _beat() -> None:
        """Print a status line whenever the stream has been quiet too long."""
        while True:
            await asyncio.sleep(_HEARTBEAT_TICK_SECONDS)
            if seconds_since_activity() >= HEARTBEAT_SECONDS:
                console.print(render_heartbeat(running.elapsed()))
                mark_activity()

    beat = asyncio.create_task(_beat())
    try:
        return await coro
    finally:
        beat.cancel()
        # Await the cancellation rather than leaving it to `asyncio.run`'s
        # shutdown sweep, so the task is provably done before the loop closes.
        with contextlib.suppress(asyncio.CancelledError):
            await beat


def drive_flow(
    start: Callable[..., Awaitable[str]],
    *,
    total_tasks: int,
    verb: str = "running",
    flow_id: str | None = None,
    resume_hint: bool = True,
) -> str:
    """Run one flow to completion on the terminal and return its id.

    ``start`` receives the engine callbacks ``on_task_event``,
    ``on_flow_started`` and ``on_task_starting`` as keyword arguments and
    returns the engine coroutine. Task banners, events, the heartbeat, the
    footer and the stopped/failed epilogue all render here.

    :param start: builds the engine coroutine from the three callbacks.
    :param total_tasks: task count for the ``[i/total]`` banner, or 0 to omit it.
    :param verb: banner verb, ``running`` or ``resuming``.
    :param flow_id: the id when resuming an existing flow. A fresh run learns
        its id from the engine and announces it.
    :param resume_hint: print ``atelier run --resume <id>`` after a failure.
    :returns: the flow id the engine returned.
    """
    collected: list[TaskEvent] = []
    captured: dict[str, str | None] = {"id": flow_id}
    running = _RunningTasks()

    def _on_event(event: TaskEvent) -> None:
        collected.append(event)
        running.finish(event)
        mark_activity()
        render_task_event(event, console)

    def _on_started(fid: str) -> None:
        captured["id"] = fid
        if flow_id is None:
            console.print(_render_orchestration_msg(f"starting flow {fid}"))

    def _on_task_starting(task_name: str, tool: str) -> None:
        index = running.start(task_name)
        mark_activity()
        console.print()
        console.print(render_task_start(task_name, tool, index, total_tasks, verb=verb))

    coro = start(
        on_task_event=_on_event,
        on_flow_started=_on_started,
        on_task_starting=_on_task_starting,
    )
    try:
        result = asyncio.run(_with_heartbeat(coro, running))
    except asyncio.CancelledError:
        # SIGTERM via `atelier stop`: the engine has marked the flow stopped.
        render_run_footer(collected, console)
        console.print("[yellow]flow stopped[/yellow]")
        if captured["id"]:
            console.print(f"[yellow]flow_id:[/yellow] {captured['id']}")
        raise typer.Exit(code=0)
    except Exception as exc:  # noqa: BLE001
        render_run_footer(collected, console)
        console.print(f"[red]flow failed:[/red] {escape(str(exc))}")
        if captured["id"]:
            console.print(f"[red]flow_id:[/red] {captured['id']}")
            if resume_hint:
                console.print(f"[dim]→ atelier run --resume {captured['id']}[/dim]")
        raise typer.Exit(code=1)
    render_run_footer(collected, console)
    console.print(f"[green]flow_id:[/green] {result}")
    return result


def _load_conduit(atelier: Atelier, name: str) -> Conduit | None:
    """Read conduit ``name``, exiting 1 with a readable message when it is malformed.

    :param atelier: Atelier whose store to read from.
    :param name: the conduit to load.
    :returns: the conduit, or ``None`` when no conduit of that name exists.
    """
    try:
        return atelier.store.read_conduit(name)
    except FileNotFoundError:
        return None
    except (yaml.YAMLError, ValidationError, ValueError) as exc:
        console.print(f"[red]invalid conduit:[/red] {escape(format_conduit_error(exc))}")
        console.print(f"[dim]→ fix conduits/{name}/conduit.yaml[/dim]")
        raise typer.Exit(code=1)


def _reject_unknown_inputs(conduit: Conduit, inputs: dict[str, str]) -> None:
    """Exit 1 when an ``--input`` key is one the conduit can never use.

    A key needs no ``inputs:`` declaration: a ``{{inputs.<key>}}`` reference
    in any task body or task input map is enough. Anything else the engine
    silently drops, so a typo would otherwise prompt again for the "missing"
    key or quietly run with a default.

    :param conduit: the loaded conduit the inputs are meant for.
    :param inputs: the parsed ``--input`` map.
    """
    accepted = accepted_input_keys(conduit)
    unknown = [k for k in inputs if k not in accepted]
    if not unknown:
        return
    for key in unknown:
        close = difflib.get_close_matches(key, sorted(accepted), n=1)
        hint = f" — did you mean {escape(close[0])}?" if close else ""
        console.print(f"[red]unknown input:[/red] {escape(key)}{hint}")
    accepts = (
        f"accepts --input: {escape(', '.join(sorted(accepted)))}"
        if accepted
        else "accepts no --input"
    )
    console.print(f"[dim]{escape(conduit.name)} {accepts}[/dim]")
    raise typer.Exit(code=1)


def _collect_missing_inputs(conduit: Conduit, inputs: dict[str, str]) -> None:
    """Prompt for each required input not yet supplied, or exit 2 off a TTY.

    :param conduit: the conduit about to run.
    :param inputs: the ``--input`` map, extended in place with typed answers.
    """
    missing = [
        k for k, spec in conduit.inputs.items() if k not in inputs and spec.default is None
    ]
    if not missing:
        return
    if not sys.stdin.isatty():
        console.print(f"[red]missing required inputs:[/red] {escape(', '.join(missing))}")
        flags = " ".join(f"--input {escape(k)}=<value>" for k in missing)
        console.print(f"[dim]→ atelier run {escape(conduit.name)} {flags}[/dim]")
        raise typer.Exit(code=2)

    from flow_atelier.cli.rendering.multiline_input import multiline_input_sync

    try:
        for key in missing:
            inputs[key] = multiline_input_sync(
                f"  {key} ({conduit.inputs[key].description}): ",
                hint="Enter to submit · Alt+Enter for newline",
            )
    except KeyboardInterrupt:
        print()
        raise typer.Exit(code=130)


@app.command(
    "run",
    help=(
        "Start a new flow for the named conduit. "
        "Use --input key=value to pass inputs, or --input-file key=path to "
        "pass a text file's contents as one input. "
        "Use --resume <flow_id> to pick up a failed or crashed run. "
        "Use --again <flow_id> to start a fresh run reusing a past flow's inputs."
    ),
)
def run_cmd(
    conduit_name: str = typer.Argument(
        None, help="Name of the conduit to run (not needed with --resume or --again)."
    ),
    inputs_raw: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="key=value input (repeatable).",
    ),
    input_files_raw: list[str] = typer.Option(
        [],
        "--input-file",
        help=(
            "key=path input read from a UTF-8 text file (repeatable). "
            "The loaded text is saved with the run, so --again replays it."
        ),
    ),
    show_steps: bool = typer.Option(
        True,
        "--show-steps/--hide-steps",
        help="Stream intermediate thinking and tool activity live (default: on).",
    ),
    resume_from: str | None = typer.Option(
        None,
        "--resume",
        help="Resume a failed or crashed flow by its id (prefix or 'latest' ok).",
    ),
    again_from: str | None = typer.Option(
        None,
        "--again",
        help=(
            "Start a fresh run of a past flow by id (prefix or 'latest' ok), "
            "reusing its saved inputs."
        ),
    ),
) -> None:
    """Start a new flow, resume a failed one, or re-run a past one.

    :param conduit_name: name of the conduit to execute.
    :param inputs_raw: list of ``key=value`` input strings collected from ``--input``.
    :param input_files_raw: list of ``key=path`` strings collected from
        ``--input-file``; each file's text becomes that key's value.
    :param show_steps: when true, stream intermediate thinking and tool activity live.
    :param resume_from: flow id (or unique prefix) of a failed run to resume.
    :param again_from: flow id (or unique prefix) of a past run to re-run from
        scratch, reusing its saved inputs (overridable via ``--input``).
    """
    atelier = Atelier()

    if resume_from is not None and again_from is not None:
        console.print("[red]error:[/red] --resume and --again are mutually exclusive")
        raise typer.Exit(code=2)

    # Resume continues the flow's saved inputs; there is nothing for a new
    # value to change. Refuse rather than read the file and ignore it.
    if resume_from is not None and input_files_raw:
        console.print(
            "[red]error:[/red] --resume and --input-file are mutually exclusive"
        )
        console.print(
            "[dim]→ --resume reuses the flow's saved inputs;"
            " use --again <flow_id> to change one[/dim]"
        )
        raise typer.Exit(code=1)

    # --resume path: resolve the old flow, skip input prompts
    if resume_from is not None:
        flow_id = _resolve_flow_id(atelier, resume_from)
        # Surface a malformed conduit with a readable message before resuming,
        # so a YAML or schema error does not reach the generic `flow failed:`
        # handler as a raw exception.
        _load_conduit(atelier, parse_flow_id(flow_id)[0])
        console.print(_render_orchestration_msg(f"resuming flow {flow_id}"))
        # No `[i/total]` on resume: the counter numbers the tasks this resume
        # starts, so pairing it with the conduit's full count would read as
        # "[1/5], just started" for a run finishing its last two tasks.
        drive_flow(
            lambda **callbacks: atelier.resume_flow(
                flow_id, show_steps=show_steps, stoppable=True, **callbacks
            ),
            total_tasks=0,
            verb="resuming",
            flow_id=flow_id,
            resume_hint=False,
        )
        return

    if again_from is not None:
        flow_id = _resolve_flow_id(atelier, again_from)
        conduit = _load_conduit(atelier, parse_flow_id(flow_id)[0])
        overrides = _parse_inputs(inputs_raw)
        overrides.update(_parse_input_files(input_files_raw, overrides))
        if conduit is not None:
            _reject_unknown_inputs(conduit, overrides)
        console.print(_render_orchestration_msg(f"re-running flow {flow_id}"))
        drive_flow(
            lambda **callbacks: atelier.rerun_flow(
                flow_id,
                overrides=overrides,
                show_steps=show_steps,
                stoppable=True,
                **callbacks,
            ),
            total_tasks=len(conduit.tasks) if conduit is not None else 0,
        )
        return

    if conduit_name is None:
        console.print(
            "[red]error:[/red] conduit name is required (unless using --resume or --again)"
        )
        raise typer.Exit(code=2)

    inputs = _parse_inputs(inputs_raw)
    inputs.update(_parse_input_files(input_files_raw, inputs))
    conduit = _load_conduit(atelier, conduit_name)
    if conduit is None:
        _exit_unknown_conduit(conduit_name, atelier.store.list_conduits())
        return

    _reject_unknown_inputs(conduit, inputs)

    # Readiness gate: refuse to start an unrunnable conduit (unregistered tool
    # or a harness CLI missing from PATH) before prompting for inputs or
    # spending any wall-clock/tokens.
    problems = atelier.tool_readiness(conduit)
    if problems:
        for problem in problems:
            console.print(f"[red]cannot run:[/red] {escape(problem)}")
        raise typer.Exit(code=1)

    _collect_missing_inputs(conduit, inputs)

    console.print(_render_orchestration_msg(f'loading conduit "{conduit_name}"'))
    drive_flow(
        lambda **callbacks: atelier.run_conduit(
            conduit_name, inputs, show_steps=show_steps, stoppable=True, **callbacks
        ),
        total_tasks=len(conduit.tasks),
    )
