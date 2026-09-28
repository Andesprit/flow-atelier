"""Compare two saved runs without loading or executing their recipes."""
from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from flow_atelier.cli._shared import (
    _flow_duration_seconds,
    _format_duration_seconds,
    _parse_iso,
    _resolve_flow_id,
    console,
)
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import display_status
from flow_atelier.modules.loop_report import loop_passes
from flow_atelier.schemas.flow import parse_flow_id


def _newest(atelier: Atelier, conduit: str) -> list[str]:
    """Find the two most recently started top-level runs of one conduit."""
    flows = atelier.list_flows(conduit)

    def started(flow_id: str) -> float:
        try:
            date = _parse_iso(atelier.store.read_progress(flow_id).started_at)
        except (OSError, ValueError):
            date = None
        return date.timestamp() if date else 0.0
    return sorted(flows, key=lambda fid: (started(fid), fid), reverse=True)[:2]


def _snapshot(atelier: Atelier, flow_id: str) -> dict:
    """Read actual task tools from logs, including every saved child flow."""
    progress = atelier.store.read_progress(flow_id)
    tasks: dict[str, dict] = {}

    def visit(current_id: str, path: str = "") -> None:
        current = atelier.store.read_progress(current_id)
        logs = atelier.store.read_logs(current_id)
        actual = {entry.task: entry.tool for entry in logs}
        passes_by_task: dict[str, list] = {}
        for item in loop_passes(atelier.store, current_id, current, logs):
            passes_by_task.setdefault(item.task, []).append(item)
        for name, task in current.tasks.items():
            key = f"{path}.{name}" if path else name
            outcome = task.loop_outcome
            passes = passes_by_task.get(name, [])
            tasks[key] = {
                "status": task.status.value,
                "agent": (
                    current.task_agents.get(name)
                    or progress.task_agents.get(key)
                    or actual.get(name)
                ),
                "loop": ({
                    "passes": len(passes) if passes else task.iteration,
                    "max_passes": task.of,
                    "condition_met": (
                        outcome.met if outcome else
                        passes[-1].condition_met if passes else None
                    ),
                } if task.of > 1 else None),
            }
        for child_id in atelier.store.list_child_flows(current_id):
            child = atelier.store.read_progress(child_id)
            if child.invoking_task:
                child_path = (
                    f"{path}.{child.invoking_task}" if path else child.invoking_task
                )
                visit(child_id, child_path)

    visit(flow_id)
    return {
        "flow_id": flow_id,
        "status": display_status(progress),
        "duration_seconds": _flow_duration_seconds(progress),
        "tasks": dict(sorted(tasks.items())),
    }


@app.command("compare")
def compare_cmd(
    first: str = typer.Argument(
        ..., help="First flow id, or a conduit name for its two newest runs."
    ),
    second: str | None = typer.Argument(None, help="Second flow id."),
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Compare agent choices, loop outcomes and duration across saved runs."""
    atelier = Atelier()
    if second is None:
        if first in atelier.list_flows():
            console.print("[red]compare needs a second flow id.[/red] "
                          "Use 'atelier compare <first-flow-id> <second-flow-id>'.")
            raise typer.Exit(code=1)
        ids = _newest(atelier, first)
        if len(ids) < 2:
            next_run = (
                f"atelier run --again {ids[0]}" if ids
                else f"atelier run {first}"
            )
            console.print(
                f"[red]compare needs two runs of {escape(first)}.[/red] "
                f"Create another with '{escape(next_run)}' "
                "or pass two flow ids."
            )
            raise typer.Exit(code=1)
        # Oldest on the left, newest on the right.
        first_id, second_id = reversed(ids)
    else:
        first_id = _resolve_flow_id(atelier, first)
        second_id = _resolve_flow_id(atelier, second)
    first_conduit = parse_flow_id(first_id)[0]
    second_conduit = parse_flow_id(second_id)[0]
    if first_conduit != second_conduit:
        console.print(
            f"[red]cannot compare different conduits:[/red] "
            f"{escape(first_conduit)} and {escape(second_conduit)}"
        )
        raise typer.Exit(code=1)
    try:
        a = _snapshot(atelier, first_id)
        b = _snapshot(atelier, second_id)
    except (OSError, ValueError) as exc:
        console.print(f"[red]cannot read saved run:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    rows = [
        {"task": name, "first": a["tasks"].get(name), "second": b["tasks"].get(name),
         "different": a["tasks"].get(name) != b["tasks"].get(name)}
        for name in sorted(a["tasks"].keys() | b["tasks"].keys())
    ]
    payload = {"conduit": first_conduit, "runs": [a, b], "tasks": rows}
    if json_mode:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"[bold]compare[/bold] {escape(first_conduit)}")
    for label, run in (("A", a), ("B", b)):
        console.print(
            f"{label} {escape(run['flow_id'])}  status={run['status']}  "
            f"duration={_format_duration_seconds(run['duration_seconds'])}"
        )
    table = Table("task", "A agent / loop", "B agent / loop", "change")
    table.columns[0].overflow = "fold"
    for row in rows:
        def describe(item: dict | None) -> str:
            if item is None:
                return "missing"
            parts = [item["agent"] or "agent unrecorded"]
            if item["status"] != "completed":
                parts.append(item["status"])
            if loop := item["loop"]:
                met = loop["condition_met"]
                condition = "met" if met is True else "not met" if met is False else "unknown"
                parts.append(f"{loop['passes']}/{loop['max_passes']} condition {condition}")
            return "\n".join(escape(str(part)) for part in parts)
        table.add_row(escape(row["task"]), describe(row["first"]),
                      describe(row["second"]), "changed" if row["different"] else "")
    console.print(table)
