"""`atelier list` sub-app commands."""
from __future__ import annotations

import json
from collections import Counter

import typer
from rich.markup import escape
from rich.table import Table

from flow_atelier.cli._shared import (
    _flow_duration_seconds,
    _format_clock,
    _format_duration_seconds,
    console,
)
from flow_atelier.cli.main import list_app
from flow_atelier.cli.rendering.render import (
    _FLOW_STATUS_STYLE,
    _task_status_summary,
    format_conduit_error,
)
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import display_status, is_crashed
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.progress import Progress


@list_app.command("conduits")
def list_conduits_cmd(
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """List all available conduits (project and global).

    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    atelier = Atelier()
    entries = atelier.store.list_conduits_with_source()

    rows: list[dict[str, object]] = []
    for name, source in entries:
        try:
            conduit = atelier.store.read_conduit(name)
            description = (
                conduit.description.splitlines()[0] if conduit.description else ""
            )
            num_tasks = len(conduit.tasks)
            num_inputs = len(conduit.inputs)
            readable = True
            error = None
        except Exception as exc:  # noqa: BLE001 — broken yaml shouldn't break list
            description = ""
            num_tasks = -1
            num_inputs = -1
            readable = False
            error = format_conduit_error(exc)
        rows.append(
            {
                "name": name,
                "source": source,
                "description": description,
                "tasks": num_tasks,
                "inputs": num_inputs,
                "readable": readable,
                "error": error,
            }
        )

    if json_mode:
        # Drop the internal `readable` key from JSON output for cleanliness.
        typer.echo(
            json.dumps([{k: v for k, v in r.items() if k != "readable"} for r in rows], indent=2)
        )
        return

    if not rows:
        console.print("[yellow]no conduits found[/yellow]")
        return
    table = Table("name", "source", "description", "tasks", "inputs")
    for r in rows:
        source_style = "cyan" if r["source"] == "project" else "magenta"
        if not r["readable"]:
            description_cell = f"[red](invalid: {escape(str(r['error']))})[/red]"
            tasks_cell = "?"
            inputs_cell = "?"
        else:
            description_cell = escape(str(r["description"]))
            tasks_cell = str(r["tasks"])
            inputs_cell = str(r["inputs"])
        table.add_row(
            str(r["name"]),
            f"[{source_style}]{r['source']}[/{source_style}]",
            description_cell,
            tasks_cell,
            inputs_cell,
        )
    console.print(table)


@list_app.command("flows")
def list_flows_cmd(
    conduit: str | None = typer.Option(
        None, "--conduit", "-c", help="Show only flows of this conduit."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """List all flows, optionally filtered by conduit.

    :param conduit: optional conduit name to filter flows by.
    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    atelier = Atelier()
    flows = atelier.list_flows(conduit)

    rows: list[dict[str, object]] = []
    progresses: dict[str, Progress | None] = {}
    for fid in flows:
        try:
            conduit_name, _uuid, _ts = parse_flow_id(fid)
        except ValueError:
            conduit_name = "?"
        progress: Progress | None
        try:
            progress = atelier.store.read_progress(fid)
        except Exception:  # noqa: BLE001
            progress = None
        progresses[fid] = progress
        if progress is None:
            rows.append(
                {
                    "flow_id": fid,
                    "conduit": conduit_name,
                    "status": None,
                    "started_at": None,
                    "finished_at": None,
                    "duration_seconds": None,
                    "task_counts": {},
                }
            )
            continue
        counts: Counter[str] = Counter(
            tp.status.value for tp in progress.tasks.values()
        )
        rows.append(
            {
                "flow_id": fid,
                "conduit": conduit_name,
                "status": progress.status.value,
                "crashed": is_crashed(progress),
                "started_at": progress.started_at,
                "finished_at": progress.finished_at,
                "duration_seconds": _flow_duration_seconds(progress),
                "task_counts": dict(counts),
            }
        )

    if json_mode:
        typer.echo(json.dumps(rows, indent=2))
        return

    if not rows:
        console.print("[yellow]no flows found[/yellow]")
        return

    table = Table()
    # flow_id must not be ellipsised: users need to copy it whole.
    table.add_column("flow_id", overflow="fold", no_wrap=False)
    for col in ("conduit", "status", "started", "duration", "tasks"):
        table.add_column(col)
    for r in rows:
        progress = progresses[str(r["flow_id"])]
        if progress is None:
            table.add_row(str(r["flow_id"]), str(r["conduit"]), "[red]?[/red]", "—", "—", "—")
            continue
        effective = display_status(progress)
        status_style = _FLOW_STATUS_STYLE.get(effective, "white")
        table.add_row(
            str(r["flow_id"]),
            str(r["conduit"]),
            f"[{status_style}]{effective}[/{status_style}]",
            _format_clock(progress.started_at),
            _format_duration_seconds(_flow_duration_seconds(progress)),
            _task_status_summary(progress),
        )
    console.print(table)
