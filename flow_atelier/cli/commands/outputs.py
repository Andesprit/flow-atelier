"""`atelier outputs` command."""
from __future__ import annotations

import json

import typer

from flow_atelier.cli._shared import _resolve_flow_id, console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier


@app.command("outputs")
def outputs_cmd(
    flow_id: str = typer.Argument(..., help="Flow id to inspect (unique prefix or 'latest' ok)."),
    task: str | None = typer.Option(
        None, "--task", "-t", help="Print only this task's raw output (pipe-friendly)."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of text blocks."
    ),
) -> None:
    """Show the per-task results a finished flow saved to ``outputs.yaml``.

    :param flow_id: flow id (or unique prefix) to inspect.
    :param task: when set, print only that task's raw value to stdout.
    :param json_mode: when true, emit machine-readable JSON.
    """
    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)
    outputs = atelier.get_outputs(flow_id)

    if task is not None:
        if task not in outputs:
            console.print(f"[red]unknown task:[/red] {task}")
            raise typer.Exit(code=1)
        value = outputs[task]
        if json_mode:
            typer.echo(json.dumps(value, indent=2))
        else:
            typer.echo("" if value is None else str(value))
        return

    if json_mode:
        typer.echo(json.dumps(outputs, indent=2))
        return

    if not outputs:
        console.print("[dim]no outputs recorded[/dim]")
        return

    for name, value in outputs.items():
        console.print(f"[bold]{name}[/bold]")
        if value is None:
            console.print("[dim](no output)[/dim]")
        else:
            typer.echo(str(value))
