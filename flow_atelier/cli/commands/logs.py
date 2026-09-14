"""`atelier logs` command (and `--follow` loop)."""
from __future__ import annotations

import json
import time
from collections import deque

import typer
from rich.markup import escape

from flow_atelier.cli._shared import (
    _flow_usage_totals,
    _format_usage,
    _resolve_flow_id,
    console,
)
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import TIMELINE_MAX_STEPS, _render_log_entry
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import is_crashed
from flow_atelier.schemas.progress import FlowStatus

_LOG_SHOW_CHOICES = ("output", "stdout", "stderr", "steps", "all")


@app.command(
    "logs",
    help="Show recorded stdout/stderr/output for each task in a flow.",
)
def logs_cmd(
    flow_id: str = typer.Argument(..., help="Flow id to inspect (unique prefix or 'latest' ok)."),
    task: str | None = typer.Option(
        None, "--task", "-t", help="Show only entries for this task."
    ),
    show: str = typer.Option(
        "output",
        "--show",
        "-s",
        help="Which channel to print: output | stdout | stderr | steps | all.",
    ),
    last: int | None = typer.Option(
        None, "--last", "-n", help="Show only the last N entries."
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Tail mode: print existing entries, then poll for new ones until the flow finishes.",
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of panels."
    ),
) -> None:
    """Show recorded stdout/stderr/output for each task iteration in a flow.

    :param flow_id: flow id (or unique prefix) to inspect.
    :param task: when set, restrict output to entries for this task.
    :param show: which channel to print (output/stdout/stderr/steps/all).
    :param last: when set, show only the last N entries.
    :param follow: when true, tail the logs until the flow finishes.
    :param json_mode: when true, emit machine-readable JSON instead of panels.
    """
    if show not in _LOG_SHOW_CHOICES:
        console.print(
            f"[red]invalid --show value:[/red] {show}  "
            f"(allowed: {', '.join(_LOG_SHOW_CHOICES)})"
        )
        raise typer.Exit(code=2)
    if follow and json_mode:
        console.print("[red]--follow and --json are mutually exclusive[/red]")
        raise typer.Exit(code=2)
    if follow and last is not None:
        console.print("[red]--follow and --last are mutually exclusive[/red]")
        raise typer.Exit(code=2)

    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)

    if follow:
        _follow_logs(atelier, flow_id, task, show)
        return

    try:
        entries = atelier.store.read_logs(flow_id)
    except FileNotFoundError:
        console.print(f"[red]unknown flow:[/red] {flow_id}")
        raise typer.Exit(code=1)

    if task is not None:
        entries = [e for e in entries if e.task == task]

    # A task only gets a LogEntry once it returns, so a stopped, crashed or
    # still-running task has none — its live step records are the only
    # record of what it did. An orphan has no stdout/stderr channel to show,
    # so an explicit request for one suppresses it; the default `output` does
    # not, since for a killed task the step timeline *is* the whole record and
    # hiding it by default would defeat the point of recording it.
    orphans = (
        {} if show in ("stdout", "stderr")
        else _orphan_steps(atelier, flow_id, entries, task)
    )

    if not entries and not orphans:
        scope = f"task {task!r}" if task else "this flow"
        if json_mode:
            typer.echo("[]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]no log entries for {escape(scope)}[/yellow]")
        raise typer.Exit(code=1)

    # `--last` bounds what gets *rendered*, so it counts entries and orphan
    # panels together in the order they are printed. Slicing only `entries`
    # would let `--last 1` print one entry plus every orphan panel.
    renderable: list = [("entry", e) for e in entries]
    renderable += [("orphan", key, steps) for key, steps in orphans.items()]
    if last is not None:
        if last < 0:
            raise typer.BadParameter("--last must be zero or positive")
        # renderable[-0:] is the whole list, so handle 0 explicitly as "none".
        renderable = renderable[-last:] if last > 0 else []
    entries = [item[1] for item in renderable if item[0] == "entry"]

    if json_mode:
        typer.echo(
            json.dumps([e.model_dump(mode="json") for e in entries], indent=2)
        )
        dropped = sum(1 for item in renderable if item[0] == "orphan")
        if dropped:
            # --json emits completed entries only; say so rather than let the
            # caller believe an in-flight task simply did nothing.
            typer.echo(
                f"note: {dropped} task iteration(s) have live steps but no "
                f"completed log entry; run without --json to see them",
                err=True,
            )
        return

    for item in renderable:
        if item[0] == "entry":
            _render_log_entry(item[1], show, console)
        else:
            (orphan_task, iteration), steps = item[1], item[2]
            _render_orphan_steps(orphan_task, iteration, steps, console)

    total_usage = _format_usage(_flow_usage_totals(entries))
    if total_usage:
        console.print(f"[dim]run total · {total_usage}[/dim]")


def _orphan_steps(
    atelier: Atelier,
    flow_id: str,
    entries: list,
    task: str | None,
) -> dict[tuple[str, int], list]:
    """Group live step records that have no matching completed log entry.

    These are the iterations that were stopped, crashed, or are still
    running — precisely the ones a post-mortem needs and the ones
    ``logs.jsonl`` cannot have.

    Each group retains only the last :data:`TIMELINE_MAX_STEPS` steps — the
    same tail the renderer would show anyway — so a long loop conduit, whose
    ``steps.jsonl`` has no rotation, cannot balloon this dict.

    :param atelier: Atelier instance used to read the step records.
    :param flow_id: full flow id to read.
    :param entries: log entries already loaded, used to exclude completed work.
    :param task: when set, restrict to this task name.
    :returns: mapping of ``(task, iteration)`` to its ordered steps.
    """
    try:
        records, _ = atelier.store.read_steps(flow_id)
    except FileNotFoundError:
        return {}
    logged = {(e.task, e.iteration) for e in entries}
    grouped: dict[tuple[str, int], deque] = {}
    for record in records:
        key = (record.task, record.iteration)
        if key in logged or (task is not None and record.task != task):
            continue
        if key not in grouped:
            grouped[key] = deque(maxlen=TIMELINE_MAX_STEPS)
        grouped[key].append(record.step)
    return {key: list(steps) for key, steps in grouped.items()}


def _render_orphan_steps(task: str, iteration: int, steps: list, console) -> None:
    """Render the step timeline of a task iteration that never completed.

    :param task: task name the steps belong to.
    :param iteration: 1-based iteration number.
    :param steps: ordered intermediate steps recorded live.
    :param console: Rich console to write the panel to.
    """
    from rich.panel import Panel
    from rich.text import Text

    from flow_atelier.cli.rendering.render import _render_steps_timeline

    suffix = f" ({iteration})" if iteration > 1 else ""
    console.print(
        Panel(
            _render_steps_timeline(steps),
            title=Text(f"⏳ {task}{suffix}", style="bold yellow"),
            title_align="left",
            subtitle="no completed log entry — stopped, crashed, or still running",
            subtitle_align="right",
            border_style="yellow",
            padding=(0, 1),
        )
    )


def _follow_logs(
    atelier: Atelier,
    flow_id: str,
    task: str | None,
    show: str,
    poll_seconds: float = 0.25,
) -> None:
    """Render existing entries, then poll for new ones until the flow ends.

    Exits cleanly when the flow's progress.json reports a terminal status
    (``completed`` or ``failed``). Exits 130 on KeyboardInterrupt.

    :param atelier: Atelier instance used to read logs and progress.
    :param flow_id: full flow id to follow.
    :param task: when set, restrict tailing to entries for this task.
    :param show: which channel to print for each entry.
    :param poll_seconds: delay between polls for new entries.
    """
    rendered = 0
    steps_offset = 0

    def _read() -> list:
        """Read all log entries for the flow, filtered by task when requested.

        :returns: list of LogEntry objects optionally filtered by task name.
        """
        try:
            entries = atelier.store.read_logs(flow_id)
        except FileNotFoundError:
            console.print(f"[red]unknown flow:[/red] {flow_id}")
            raise typer.Exit(code=1)
        if task is not None:
            entries = [e for e in entries if e.task == task]
        return entries

    def _drain_and_render() -> None:
        """Render log entries and live steps written since the last pass.

        Steps come first: they arrive while a task is still running, whereas
        its log entry only lands once it returns. Tailing them is what makes
        a long agent task watchable from a second terminal.
        """
        nonlocal rendered, steps_offset
        from flow_atelier.cli.rendering.render import _render_step

        try:
            # Resume from where the last poll stopped: steps.jsonl has no
            # rotation, so re-reading it whole every poll costs more the
            # longer the flow runs.
            fresh, steps_offset = atelier.store.read_steps(flow_id, steps_offset)
        except FileNotFoundError:
            fresh = []
        for record in fresh:
            if task is not None and record.task != task:
                continue
            console.print(_render_step(record.step, task=record.task))

        entries = _read()
        for entry in entries[rendered:]:
            _render_log_entry(entry, show, console)
        rendered = len(entries)

    try:
        while True:
            _drain_and_render()
            try:
                progress = atelier.store.read_progress(flow_id)
            except FileNotFoundError:
                # Flow vanished mid-stream; nothing more to do.
                return
            if progress.status != FlowStatus.running:
                # Final pass to capture any entries written between the last
                # read and the terminal state transition.
                _drain_and_render()
                return
            if is_crashed(progress):
                # Runner is provably dead but progress.json is frozen at
                # running; drain and stop rather than polling forever.
                _drain_and_render()
                console.print(
                    "[dim]— flow runner is no longer alive (crashed) —[/dim]"
                )
                return
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        console.print("[dim]— follow interrupted —[/dim]")
        raise typer.Exit(code=130)
