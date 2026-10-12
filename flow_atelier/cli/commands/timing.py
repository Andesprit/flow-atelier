"""`atelier timing` command — per-task timing breakdown for a flow."""
from __future__ import annotations

import json
from datetime import datetime

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
    iterations so a looped step shows its true total cost. Task time is
    summed, so parallel tasks add up to more than the run took; the wall
    time is the span from the first start to the last finish. Agent tasks
    also split their time into startup (spawn to a usable session) and the
    wait for the agent's first output.

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
        agg = totals.setdefault(
            entry.task, {"total": 0.0, "runs": 0, "startup": 0.0, "first_output": 0.0}
        )
        agg["total"] += entry.duration_seconds
        agg["runs"] += 1
        timings = entry.extra.get("timings") or {}
        agg["startup"] += timings.get("startup_seconds", 0.0)
        agg["first_output"] += timings.get("first_output_seconds", 0.0)

    flow_total = sum(agg["total"] for agg in totals.values())
    startup_total = sum(agg["startup"] for agg in totals.values())
    wall = _wall_seconds(entries)
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1]["total"], kv[0]))

    if json_mode:
        payload = {
            "flow_id": flow_id,
            "total_seconds": flow_total,
            "wall_seconds": wall,
            "startup_seconds": round(startup_total, 3),
            "tasks": [
                {
                    "task": task,
                    "total_seconds": agg["total"],
                    "runs": int(agg["runs"]),
                    "pct": (agg["total"] / flow_total * 100) if flow_total else 0.0,
                    "startup_seconds": round(agg["startup"], 3),
                    "first_output_seconds": round(agg["first_output"], 3),
                }
                for task, agg in ordered
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(
        f"[bold]flow[/bold] {flow_id}  "
        f"wall={_format_duration_seconds(wall)}  "
        f"task time={_format_duration_seconds(flow_total)}"
    )
    table = Table("task", "duration", "runs", "pct", "startup", "to first output")
    for task, agg in ordered:
        share = (agg["total"] / flow_total * 100) if flow_total else 0.0
        table.add_row(
            task,
            _format_duration_seconds(agg["total"]),
            str(int(agg["runs"])),
            f"{share:.0f}%",
            _format_duration_seconds(agg["startup"]) if agg["startup"] else "",
            _format_duration_seconds(agg["first_output"]) if agg["first_output"] else "",
        )
    console.print(table)
    if startup_total:
        console.print(
            f"[dim]agent startup: {_format_duration_seconds(startup_total)} "
            f"of {_format_duration_seconds(flow_total)} task time[/dim]"
        )


def _wall_seconds(entries) -> float | None:
    """Return the span from the earliest start to the latest finish.

    :param entries: the flow's log entries.
    :returns: seconds between the first ``started_at`` and the last
        ``finished_at``, or ``None`` when a timestamp does not parse.
    """
    try:
        starts = [datetime.fromisoformat(e.started_at) for e in entries]
        ends = [datetime.fromisoformat(e.finished_at) for e in entries]
    except ValueError:
        return None
    return round((max(ends) - min(starts)).total_seconds(), 3)
