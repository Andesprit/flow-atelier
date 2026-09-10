"""`atelier timing` command — per-task timing breakdown for a flow."""
from __future__ import annotations

import json

import typer
from rich.table import Table

from flow_atelier.cli._shared import (
    _format_duration_seconds,
    _resolve_flow_id,
    console,
)
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier


@app.command("timing")
def timing_cmd(
    flow_id: str = typer.Argument(..., help="Flow id to inspect (unique prefix or 'latest' ok)."),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Show a per-task timing breakdown for a flow, slowest task first.

    Reads the per-iteration ``duration_seconds`` already persisted in the
    flow's ``logs.jsonl`` and aggregates by task name, summing repeated
    iterations so a looped step shows its true total cost.

    :param flow_id: flow id (or unique prefix) to inspect.
    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)

    try:
        entries = atelier.store.read_logs(flow_id)
    except FileNotFoundError:
        console.print(f"[red]unknown flow:[/red] {flow_id}")
        raise typer.Exit(code=1)

    if not entries:
        if json_mode:
            typer.echo("[]")
            raise typer.Exit(code=1)
        console.print("[yellow]no log entries for this flow[/yellow]")
        raise typer.Exit(code=1)

    totals: dict[str, dict[str, float]] = {}
    for entry in entries:
        agg = totals.setdefault(entry.task, {"total": 0.0, "runs": 0})
        agg["total"] += entry.duration_seconds
        agg["runs"] += 1

    flow_total = sum(agg["total"] for agg in totals.values())
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1]["total"], kv[0]))

    if json_mode:
        payload = {
            "flow_id": flow_id,
            "total_seconds": flow_total,
            "tasks": [
                {
                    "task": task,
                    "total_seconds": agg["total"],
                    "runs": int(agg["runs"]),
                    "pct": (agg["total"] / flow_total * 100) if flow_total else 0.0,
                }
                for task, agg in ordered
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(
        f"[bold]flow[/bold] {flow_id}  "
        f"total={_format_duration_seconds(flow_total)}"
    )
    table = Table("task", "duration", "runs", "pct")
    for task, agg in ordered:
        share = (agg["total"] / flow_total * 100) if flow_total else 0.0
        table.add_row(
            task,
            _format_duration_seconds(agg["total"]),
            str(int(agg["runs"])),
            f"{share:.0f}%",
        )
    console.print(table)
