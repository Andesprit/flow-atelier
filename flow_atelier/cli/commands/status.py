"""`atelier status` command."""
from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from flow_atelier.cli._shared import (
    _flow_duration_seconds,
    _flow_usage_totals,
    _format_clock,
    _format_duration_seconds,
    _format_usage,
    _resolve_flow_id,
    console,
)
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import _FLOW_STATUS_STYLE, _task_status_summary
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import display_status, is_crashed
from flow_atelier.modules.loop_report import loop_pass_lines, loop_passes, unmet_loop_messages
from flow_atelier.schemas.conduit import display_conduit_name
from flow_atelier.schemas.flow import parse_flow_id


@app.command("status")
def status_cmd(
    flow_id: str = typer.Argument(..., help="Flow id to inspect (unique prefix or 'latest' ok)."),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Show progress for a flow.

    :param flow_id: flow id (or unique prefix) to inspect.
    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)
    try:
        progress = atelier.get_status(flow_id)
    except FileNotFoundError:
        console.print(f"[red]unknown flow:[/red] {flow_id}")
        raise typer.Exit(code=1)

    logs = atelier.get_flow_logs(flow_id)
    passes = loop_passes(atelier.store, flow_id, progress, logs)
    usage_totals = _flow_usage_totals(logs)

    if json_mode:
        payload = progress.model_dump(mode="json")
        for task in payload["tasks"].values():
            if task.get("loop_outcome") is None:
                task.pop("loop_outcome", None)
        payload["flow_id"] = flow_id
        payload["duration_seconds"] = _flow_duration_seconds(progress)
        payload["crashed"] = is_crashed(progress)
        payload["usage"] = (
            usage_totals.model_dump(mode="json") if usage_totals else None
        )
        if warnings := unmet_loop_messages(progress):
            payload["loop_warnings"] = warnings
        if passes:
            payload["loop_passes"] = [item.model_dump(mode="json") for item in passes]
        typer.echo(json.dumps(payload, indent=2))
        return

    effective = display_status(progress)
    flow_status_style = _FLOW_STATUS_STYLE.get(effective, "white")
    duration = _flow_duration_seconds(progress)
    conduit_name, _, _ = parse_flow_id(flow_id)
    display_name = display_conduit_name(conduit_name)
    display_part = (
        f"[dim]({escape(display_name)})[/dim]  "
        if display_name != conduit_name else ""
    )
    header = (
        f"[bold]flow[/bold] {flow_id}  "
        f"{display_part}status=[{flow_status_style}]{effective}[/{flow_status_style}]  "
        f"started={_format_clock(progress.started_at)}  "
        f"duration={_format_duration_seconds(duration)}"
    )
    usage_line = _format_usage(usage_totals)
    if usage_line:
        header += f"  {usage_line}"
    console.print(header)
    for warning in unmet_loop_messages(progress):
        console.print(f"[yellow]⚠ {escape(warning)}[/yellow]")
    if is_crashed(progress):
        console.print(f"[dim]→ atelier run --resume {flow_id}[/dim]")

    show_iteration = any(tp.of > 1 for tp in progress.tasks.values())
    # Only a run that chose an agent — at launch, or by keeping the one a
    # resumed step actually ran on — has anything to say here; every other
    # run's agents are the recipe's, and a column of blanks says nothing.
    show_agent = bool(progress.task_agents)
    # The other tasks ran on the recipe's own choice; the log says which.
    # Entries tagged with a flow_id are a nested run's, not these rows'.
    ran_on = {e.task: e.tool for e in logs if "flow_id" not in e.extra}
    spaces = progress.workspaces
    columns = ["task", "status"]
    if show_agent:
        columns.append("agent")
    if spaces is not None:
        columns.append("checkout")
    if show_iteration:
        columns.append("iteration")
    columns.append("reason")
    table = Table(*columns)
    if spaces is not None:
        # A path is copied, not skimmed: wrap it rather than cut it with "…".
        table.columns[columns.index("checkout")].overflow = "fold"
    for name, tp in progress.tasks.items():
        row = [escape(name), tp.status.value]
        if show_agent:
            chosen = progress.task_agents.get(name)
            inner = sorted(
                (path[len(name) + 1:], tool)
                for path, tool in progress.task_agents.items()
                if path.startswith(f"{name}.")
            )
            if chosen:
                row.append(escape(chosen))
            elif inner:
                # A call's own tool says nothing; name what its body runs on.
                row.append(escape(", ".join(f"{path}: {tool}" for path, tool in inner)))
            elif name in ran_on:
                row.append(f"{escape(ran_on[name])} [dim](recipe)[/dim]")
            else:
                row.append("[dim](recipe)[/dim]")
        if spaces is not None:
            row.append(escape(spaces.paths.get(name, "")))
        if show_iteration:
            row.append(f"{tp.iteration}/{tp.of}" if tp.of > 1 else "")
        row.append(escape(tp.reason or ""))
        table.add_row(*row)
    if show_agent:
        console.print(
            "[dim]agent: what this run uses for that task, whether chosen with "
            "--agent or recorded from the run that produced its result; "
            "(recipe) marks the conduit's own choice, and the conduit file is "
            "unchanged[/dim]"
        )
        nested = {k: v for k, v in progress.task_agents.items() if "." in k}
        if nested:
            console.print("[bold]Nested agent choices[/bold]")
            for path, tool in sorted(nested.items()):
                console.print(f"  {escape(path)}: {escape(tool)}")
    if spaces is not None:
        console.print(
            f"[dim]checkout: this task's own Git worktree, cut from "
            f"{escape(spaces.source)} at {escape(spaces.base[:12])}; kept after "
            f"the run, never merged back, and not removed by `atelier rm`. A "
            f"blank cell means the task ran in the shared working "
            f"directory.[/dim]"
        )
    console.print(table)
    if passes:
        console.print("[bold]loop passes[/bold]")
        for line in loop_pass_lines(passes):
            console.print(f"  {escape(line)}")
    console.print(_task_status_summary(progress))
