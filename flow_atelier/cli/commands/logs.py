"""`atelier logs` command (and `--follow` loop)."""
from __future__ import annotations

import json
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from heapq import merge

import typer

from flow_atelier.cli._shared import (
    _flow_usage_totals,
    _format_usage,
    _resolve_flow_id,
    cli_error,
    console,
)
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import TIMELINE_MAX_STEPS, _render_log_entry, _render_step
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import display_status
from flow_atelier.schemas.log import LogEntry, StepKind, StepRecord

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
    show: str | None = typer.Option(
        None,
        "--show",
        "-s",
        help="Channel: output | stdout | stderr | steps | all. Default: output (all for --jsonl).",
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
    jsonl: bool = typer.Option(
        False, "--jsonl", help="Emit one JSON object per step, completed entry and stream end."
    ),
) -> None:
    """Show recorded stdout/stderr/output for each task iteration in a flow.

    :param flow_id: flow id (or unique prefix) to inspect.
    :param task: when set, restrict output to entries for this task.
    :param show: which channel to print (output/stdout/stderr/steps/all).
    :param last: when set, show only the last N entries.
    :param follow: when true, tail the logs until the flow finishes.
    :param json_mode: when true, emit machine-readable JSON instead of panels.
    :param jsonl: when true, stream newline-delimited JSON records.
    """
    if show is None:
        show = "all" if jsonl else "output"
    if show not in _LOG_SHOW_CHOICES:
        raise cli_error(
            "invalid_arguments",
            f"invalid --show value: {show}  (allowed: {', '.join(_LOG_SHOW_CHOICES)})",
            exit_code=2, json_mode=json_mode or jsonl, out=console,
            parameter="--show", allowed=_LOG_SHOW_CHOICES,
        )
    if json_mode and jsonl:
        raise cli_error(
            "invalid_arguments", "--json and --jsonl are mutually exclusive",
            exit_code=2, json_mode=True, parameter="--jsonl",
        )
    if follow and json_mode:
        raise cli_error(
            "invalid_arguments", "--follow and --json are mutually exclusive",
            exit_code=2, json_mode=True, parameter="--follow",
        )
    if follow and last is not None:
        raise cli_error(
            "invalid_arguments", "--follow and --last are mutually exclusive",
            exit_code=2, json_mode=jsonl, out=console, parameter="--last",
        )
    if last is not None and last < 0:
        raise typer.BadParameter("--last must be zero or positive", param_hint="--last")

    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id, json_mode=json_mode or jsonl)

    if follow or jsonl:
        _stream_logs(atelier, flow_id, task, show, follow, jsonl, last)
        return

    try:
        entries = atelier.store.read_logs(flow_id)
    except FileNotFoundError:
        raise cli_error(
            "unknown_flow", f"unknown flow: {flow_id}", json_mode=json_mode,
            out=console, flow_id=flow_id,
        )

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
        raise cli_error(
            "no_log_entries", f"no log entries for {scope}",
            json_mode=json_mode, out=console, flow_id=flow_id,
        )

    # `--last` bounds what gets *rendered*, so it counts entries and orphan
    # panels together in the order they are printed. Slicing only `entries`
    # would let `--last 1` print one entry plus every orphan panel.
    renderable: list = [("entry", e) for e in entries]
    renderable += [("orphan", key, steps) for key, steps in orphans.items()]
    if last is not None:
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


def _jsonl_records(
    steps: list[StepRecord],
    entries: list[LogEntry],
    show: str,
    live_iterations: set[tuple[str, int]],
) -> Iterator[dict]:
    """Interleave the two append-only journals by timestamp within this poll."""
    def timestamp(record: StepRecord | LogEntry) -> str:
        return record.step.timestamp if isinstance(record, StepRecord) else record.finished_at

    live_iterations.update((step.task, step.iteration) for step in steps)
    for record in merge(steps, entries, key=timestamp):
        if isinstance(record, StepRecord):
            if show in ("steps", "all") or show == record.step.kind.value:
                yield {"kind": "step", **record.model_dump(mode="json")}
            continue

        # Older runs stored their timeline only inside the completed entry.
        if (record.task, record.iteration) not in live_iterations and show != "output":
            for step in record.steps:
                if show in ("steps", "all") or show == step.kind.value:
                    yield {
                        "kind": "step", "task": record.task, "iteration": record.iteration,
                        "step": step.model_dump(mode="json"),
                    }
        if show != "steps":
            fields = None if show == "all" else {"task", "iteration", "exit_code", show}
            entry = record.model_dump(mode="json", include=fields, exclude={"steps"})
            yield {"kind": "entry", **entry}


def _stream_logs(
    atelier: Atelier, flow_id: str, task: str | None, show: str,
    follow: bool, jsonl: bool, last: int | None,
) -> None:
    """Drain recorded work once, or poll the same cursors until termination."""
    entries_offset = steps_offset = 0
    live_iterations: set[tuple[str, int]] = set()
    interrupted = False
    writing = False

    def request_interrupt(signum, frame):
        nonlocal interrupted
        if not writing:
            raise KeyboardInterrupt
        interrupted = True

    previous_handler = None
    if jsonl and threading.current_thread() is threading.main_thread():
        # A pipe write can span several buffers. Finish its JSON line before
        # acting on Ctrl-C so the end record always starts on a new line.
        previous_handler = signal.signal(signal.SIGINT, request_interrupt)
    try:
        while True:
            # Read progress first so observing a terminal state always leads
            # to a final drain of everything the runner wrote before it ended.
            try:
                status = display_status(atelier.store.read_progress(flow_id))
            except FileNotFoundError:
                status = "unknown"

            try:
                entries = atelier.store.read_logs(flow_id)
                steps, steps_offset = atelier.store.read_steps(flow_id, steps_offset)
            except FileNotFoundError:
                raise cli_error(
                    "unknown_flow", f"unknown flow: {flow_id}", json_mode=jsonl,
                    out=console, flow_id=flow_id,
                )
            fresh_entries = entries[entries_offset:]
            entries_offset = len(entries)
            if task is not None:
                steps = [record for record in steps if record.task == task]
                fresh_entries = [entry for entry in fresh_entries if entry.task == task]

            if jsonl:
                records = _jsonl_records(steps, fresh_entries, show, live_iterations)
                if last is not None:
                    records = iter(deque(records, maxlen=min(last, sys.maxsize)))
                for item in records:
                    line = json.dumps(item, allow_nan=False)
                    writing = True
                    typer.echo(line)
                    writing = False
                    if interrupted:
                        raise KeyboardInterrupt
            else:
                for record in steps:
                    # A shell task's entry prints these lines when it ends.
                    if record.step.kind not in (StepKind.stdout, StepKind.stderr):
                        console.print(_render_step(record.step, task=record.task))
                for entry in fresh_entries:
                    _render_log_entry(entry, show, console)

            if not follow or status != "running":
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        status = "interrupted"
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)

    if jsonl:
        typer.echo(json.dumps({"kind": "end", "flow_id": flow_id, "status": status}))
    elif status == "crashed":
        console.print("[dim]— flow runner is no longer alive (crashed) —[/dim]")
    elif status == "interrupted":
        console.print("[dim]— follow interrupted —[/dim]")
    if status == "interrupted":
        raise typer.Exit(code=130)
