"""`atelier run` command."""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import sys
import time

import typer
import yaml
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import (
    _exit_unknown_conduit,
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


@app.command(
    "run",
    help=(
        "Start a new flow for the named conduit. "
        "Use --input key=value to pass inputs. "
        "Use --resume <flow_id> to pick up a failed or crashed run. "
        "Use --again <flow_id> to start a fresh run reusing a past flow's inputs."
    ),
)
def run_cmd(
    conduit_name: str = typer.Argument(
        None, help="Name of the conduit to run (not needed with --resume)."
    ),
    inputs_raw: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="key=value input (repeatable).",
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
    :param show_steps: when true, stream intermediate thinking and tool activity live.
    :param resume_from: flow id (or unique prefix) of a failed run to resume.
    :param again_from: flow id (or unique prefix) of a past run to re-run from
        scratch, reusing its saved inputs (overridable via ``--input``).
    """
    atelier = Atelier()

    if resume_from is not None and again_from is not None:
        console.print("[red]error:[/red] --resume and --again are mutually exclusive")
        raise typer.Exit(code=1)

    # --resume path: resolve the old flow, skip input prompts
    if resume_from is not None:
        flow_id = _resolve_flow_id(atelier, resume_from)
        # Surface a malformed conduit with a readable message before resuming,
        # so a YAML/schema/name error doesn't reach the generic `flow failed:`
        # handler as a raw exception. read_conduit is the only conduit-load
        # surface; resume_flow's own ValueError (e.g. "can only resume failed
        # or crashed flows") is unrelated and must keep its generic handling.
        resume_conduit = parse_flow_id(flow_id)[0]
        # No `[i/total]` on resume. `_RunningTasks.count` numbers the tasks
        # started *by this resume*, so pairing it with the conduit's full task
        # count reads as "[1/5], just started" for a run that is finishing its
        # last two tasks. Validate the conduit anyway — the error handling
        # below is the point of the read.
        total_tasks = 0
        try:
            atelier.store.read_conduit(resume_conduit)
        except FileNotFoundError:
            pass
        except (yaml.YAMLError, ValidationError, ValueError) as exc:
            console.print(
                f"[red]invalid conduit:[/red] {escape(format_conduit_error(exc))}"
            )
            console.print(f"[dim]→ fix conduits/{resume_conduit}/conduit.yaml[/dim]")
            raise typer.Exit(code=1)
        collected_events: list[TaskEvent] = []
        captured_flow_id: dict[str, str | None] = {"id": flow_id}

        running = _RunningTasks()

        def _on_event(event: TaskEvent) -> None:
            collected_events.append(event)
            running.finish(event)
            mark_activity()
            render_task_event(event, console)

        def _on_started(fid: str) -> None:
            captured_flow_id["id"] = fid

        def _on_task_starting(task_name: str, tool: str) -> None:
            index = running.start(task_name)
            mark_activity()
            console.print()
            console.print(
                render_task_start(
                    task_name, tool, index, total_tasks, verb="resuming"
                )
            )

        console.print(_render_orchestration_msg(f'resuming flow {flow_id}'))
        try:
            result_id = asyncio.run(
                _with_heartbeat(
                    atelier.resume_flow(
                        flow_id,
                        on_task_event=_on_event,
                        on_flow_started=_on_started,
                        on_task_starting=_on_task_starting,
                        show_steps=show_steps,
                        stoppable=True,
                    ),
                    running,
                )
            )
        except asyncio.CancelledError:
            # SIGTERM via `atelier stop`: engine has marked the flow stopped.
            render_run_footer(collected_events, console)
            console.print("[yellow]flow stopped[/yellow]")
            stopped_id = captured_flow_id["id"]
            if stopped_id:
                console.print(f"[yellow]flow_id:[/yellow] {stopped_id}")
            raise typer.Exit(code=0)
        except Exception as e:  # noqa: BLE001
            render_run_footer(collected_events, console)
            console.print(f"[red]flow failed:[/red] {escape(str(e))}")
            raise typer.Exit(code=1)
        render_run_footer(collected_events, console)
        console.print(f"[green]flow_id:[/green] {result_id}")
        return

    # --again path: start a fresh run reusing a past flow's saved inputs
    if again_from is not None:
        flow_id = _resolve_flow_id(atelier, again_from)
        again_conduit = parse_flow_id(flow_id)[0]
        total_tasks = 0
        again_def: Conduit | None = None
        try:
            again_def = atelier.store.read_conduit(again_conduit)
            total_tasks = len(again_def.tasks)
        except FileNotFoundError:
            pass
        except (yaml.YAMLError, ValidationError, ValueError) as exc:
            console.print(
                f"[red]invalid conduit:[/red] {escape(format_conduit_error(exc))}"
            )
            console.print(f"[dim]→ fix conduits/{again_conduit}/conduit.yaml[/dim]")
            raise typer.Exit(code=1)
        overrides = _parse_inputs(inputs_raw)
        if again_def is not None:
            _reject_unknown_inputs(again_def, overrides)
        collected_events = []
        captured_flow_id = {"id": None}
        running = _RunningTasks()

        def _on_event(event: TaskEvent) -> None:
            collected_events.append(event)
            running.finish(event)
            mark_activity()
            render_task_event(event, console)

        def _on_started(fid: str) -> None:
            captured_flow_id["id"] = fid
            console.print(_render_orchestration_msg(f'starting flow {fid}'))

        def _on_task_starting(task_name: str, tool: str) -> None:
            index = running.start(task_name)
            mark_activity()
            console.print()
            console.print(
                render_task_start(task_name, tool, index, total_tasks)
            )

        console.print(_render_orchestration_msg(f're-running flow {flow_id}'))
        try:
            result_id = asyncio.run(
                _with_heartbeat(
                    atelier.rerun_flow(
                        flow_id,
                        overrides=overrides,
                        on_task_event=_on_event,
                        on_flow_started=_on_started,
                        on_task_starting=_on_task_starting,
                        show_steps=show_steps,
                        stoppable=True,
                    ),
                    running,
                )
            )
        except asyncio.CancelledError:
            render_run_footer(collected_events, console)
            console.print("[yellow]flow stopped[/yellow]")
            stopped_id = captured_flow_id["id"]
            if stopped_id:
                console.print(f"[yellow]flow_id:[/yellow] {stopped_id}")
            raise typer.Exit(code=0)
        except Exception as e:  # noqa: BLE001
            render_run_footer(collected_events, console)
            console.print(f"[red]flow failed:[/red] {escape(str(e))}")
            fid = captured_flow_id["id"]
            if fid:
                console.print(f"[red]flow_id:[/red] {fid}")
                console.print(f"[dim]→ atelier run --resume {fid}[/dim]")
            raise typer.Exit(code=1)
        render_run_footer(collected_events, console)
        console.print(f"[green]flow_id:[/green] {result_id}")
        return

    # --- normal run path (no --resume) ---

    if conduit_name is None:
        console.print("[red]error:[/red] conduit name is required (unless using --resume)")
        raise typer.Exit(code=1)

    inputs = _parse_inputs(inputs_raw)

    try:
        conduit = atelier.store.read_conduit(conduit_name)
    except FileNotFoundError:
        _exit_unknown_conduit(conduit_name, atelier.store.list_conduits())
    except (yaml.YAMLError, ValidationError, ValueError) as exc:
        console.print(
            f"[red]invalid conduit:[/red] {escape(format_conduit_error(exc))}"
        )
        console.print(f"[dim]→ fix conduits/{conduit_name}/conduit.yaml[/dim]")
        raise typer.Exit(code=1)

    _reject_unknown_inputs(conduit, inputs)

    # Readiness gate: refuse to start an unrunnable conduit (unregistered tool
    # or a harness CLI missing from PATH) before prompting for inputs or
    # spending any wall-clock/tokens.
    problems = atelier.tool_readiness(conduit)
    if problems:
        for problem in problems:
            console.print(f"[red]cannot run:[/red] {escape(problem)}")
        raise typer.Exit(code=1)

    # Prompt for missing inputs when running interactively.
    missing = [
        k
        for k, spec in conduit.inputs.items()
        if k not in inputs and spec.default is None
    ]
    if missing and sys.stdin.isatty():
        from flow_atelier.cli.rendering.multiline_input import multiline_input_sync

        try:
            for key in missing:
                value = multiline_input_sync(
                    f"  {key} ({conduit.inputs[key].description}): ",
                    hint="Enter to submit · Alt+Enter for newline",
                )
                inputs[key] = value
        except KeyboardInterrupt:
            print()
            raise typer.Exit(code=130)

    collected_events = []
    captured_flow_id = {"id": None}
    running = _RunningTasks()

    def _on_event(event: TaskEvent) -> None:
        """Collect the task event and render it to the console.

        :param event: the emitted task event to record and display.
        """
        collected_events.append(event)
        running.finish(event)
        mark_activity()
        render_task_event(event, console)

    def _on_started(fid: str) -> None:
        """Capture the flow id and print a start banner.

        :param fid: the flow id assigned when the run begins.
        """
        captured_flow_id["id"] = fid
        console.print(_render_orchestration_msg(f'starting flow {fid}'))

    def _on_task_starting(task_name: str, tool: str) -> None:
        """Announce that a task is about to run.

        :param task_name: name of the task starting.
        :param tool: tool identifier used to execute the task.
        """
        index = running.start(task_name)
        mark_activity()
        console.print()
        console.print(
            render_task_start(task_name, tool, index, len(conduit.tasks))
        )

    console.print(_render_orchestration_msg(f'loading conduit "{conduit_name}"'))

    try:
        flow_id = asyncio.run(
            _with_heartbeat(
                atelier.run_conduit(
                    conduit_name,
                    inputs,
                    on_task_event=_on_event,
                    on_flow_started=_on_started,
                    on_task_starting=_on_task_starting,
                    show_steps=show_steps,
                    stoppable=True,
                ),
                running,
            )
        )
    except asyncio.CancelledError:
        # SIGTERM via `atelier stop`: engine has marked the flow stopped.
        render_run_footer(collected_events, console)
        console.print("[yellow]flow stopped[/yellow]")
        stopped_id = captured_flow_id["id"]
        if stopped_id:
            console.print(f"[yellow]flow_id:[/yellow] {stopped_id}")
        raise typer.Exit(code=0)
    except Exception as e:  # noqa: BLE001
        render_run_footer(collected_events, console)
        console.print(f"[red]flow failed:[/red] {escape(str(e))}")
        fid = captured_flow_id["id"]
        if fid:
            console.print(f"[red]flow_id:[/red] {fid}")
            console.print(f"[dim]→ atelier run --resume {fid}[/dim]")
        raise typer.Exit(code=1)
    render_run_footer(collected_events, console)
    console.print(f"[green]flow_id:[/green] {flow_id}")
