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

    usage_totals = _flow_usage_totals(atelier.get_flow_logs(flow_id))

    if json_mode:
        payload = progress.model_dump(mode="json")
        payload["flow_id"] = flow_id
        payload["duration_seconds"] = _flow_duration_seconds(progress)
        payload["crashed"] = is_crashed(progress)
        payload["usage"] = (
            usage_totals.model_dump(mode="json") if usage_totals else None
        )
        typer.echo(json.dumps(payload, indent=2))
        return

    effective = display_status(progress)
    flow_status_style = _FLOW_STATUS_STYLE.get(effective, "white")
    duration = _flow_duration_seconds(progress)
    header = (
        f"[bold]flow[/bold] {flow_id}  "
        f"status=[{flow_status_style}]{effective}[/{flow_status_style}]  "
        f"started={_format_clock(progress.started_at)}  "
        f"duration={_format_duration_seconds(duration)}"
    )
    usage_line = _format_usage(usage_totals)
    if usage_line:
        header += f"  {usage_line}"
    console.print(header)
    if is_crashed(progress):
        console.print(f"[dim]→ atelier run --resume {flow_id}[/dim]")

    show_iteration = any(tp.of > 1 for tp in progress.tasks.values())
    columns = ["task", "status"]
    if show_iteration:
        columns.append("iteration")
    columns.append("reason")
    table = Table(*columns)
    for name, tp in progress.tasks.items():
        row = [escape(name), tp.status.value]
        if show_iteration:
            row.append(f"{tp.iteration}/{tp.of}" if tp.of > 1 else "")
        row.append(escape(tp.reason or ""))
        table.add_row(*row)
    console.print(table)
    console.print(_task_status_summary(progress))
