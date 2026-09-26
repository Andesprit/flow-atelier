"""`atelier schedule` sub-app commands."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from flow_atelier.cli._shared import _format_clock, _schedule_store, console
from flow_atelier.cli.main import list_app, schedule_app
from flow_atelier.cli.rendering.render import (
    render_planned_table,
    render_run_footer,
    render_task_event,
)
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.api import CreateScheduleInput
from flow_atelier.schemas.log import TaskEvent
from flow_atelier.services.scheduler import (
    ScheduleStore,
    compute_planned_view,
    default_local_zone,
)


def _resolve_schedule(store: ScheduleStore, ref: str):
    """Find a schedule by id, then fall back to a name lookup.

    :param store: schedule store to query.
    :param ref: schedule id or ``schedule.name`` value.
    :returns: the matched schedule, or None when nothing matches.
    """
    job = store.get(ref)
    if job is not None:
        return job
    return store.get_by_name(ref)


def _load_schedule_payload(path: Path) -> CreateScheduleInput:
    """Load YAML or JSON containing a CreateScheduleInput shape.

    :param path: filesystem path to a JSON or YAML schedule file.
    :returns: a validated :class:`CreateScheduleInput`.
    """
    text = path.read_text()
    suffix = path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(text)
    else:
        import yaml

        raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("schedule file must contain a JSON/YAML mapping")
    return CreateScheduleInput.model_validate(raw)


@schedule_app.command(
    "add",
    help="Install a schedule (JSON or YAML) into .atelier/schedules/.",
)
def schedule_add_cmd(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Path to a schedule JSON or YAML file."
    ),
) -> None:
    """Validate and persist a schedule via the JSON store.

    :param file: path to a schedule JSON or YAML file to install.
    """
    try:
        payload = _load_schedule_payload(file)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]invalid schedule:[/red] {e}")
        raise typer.Exit(code=1)
    # Route through the facade so conduit_name is validated against the store
    # (a typo'd conduit otherwise installs cleanly and only fails at fire time).
    try:
        job = Atelier().create_schedule(payload)
    except (ValueError, FileExistsError) as e:
        console.print(f"[red]invalid schedule:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]installed[/green] {job.id}")


@list_app.command("schedules", help="List installed schedules and their next fire times.")
@schedule_app.command("list", hidden=True)
def schedule_list_cmd(
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """List installed schedules and their next fire times.

    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    store = _schedule_store()
    planned = compute_planned_view(
        store,
        default_zone=default_local_zone(),
        default_working_dir=Path.cwd(),
    )

    if json_mode:
        payload = {
            "schedules": [
                {
                    "id": p.id,
                    "name": p.name,
                    "conduit": p.conduit_name,
                    "kind": p.schedule_kind,
                    "next_fire_time": (
                        p.next_fire_time.isoformat() if p.next_fire_time else None
                    ),
                    "last_run": (
                        p.last_run.model_dump(mode="json") if p.last_run else None
                    ),
                    "working_dir": str(p.working_dir),
                }
                for p in planned
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    if not planned:
        console.print("[yellow]no schedules found[/yellow]")
        return

    console.print(render_planned_table(planned))


@schedule_app.command("rm", help="Delete a schedule by id or name (hard delete).")
@schedule_app.command("remove", hidden=True)
def schedule_remove_cmd(
    ref: str = typer.Argument(..., help="Schedule id or schedule.name."),
) -> None:
    """Delete a schedule by id or name; the YAML file is unlinked.

    :param ref: schedule id or ``schedule.name`` to remove.
    """
    store = _schedule_store()
    job = _resolve_schedule(store, ref)
    if job is None:
        console.print(f"[yellow]schedule not found:[/yellow] {ref}")
        raise typer.Exit(code=1)
    store.delete(job.id)
    console.print(f"[green]removed[/green] {job.id}")


@schedule_app.command(
    "history",
    help="Show a schedule's recorded run history (newest first).",
)
def schedule_history_cmd(
    ref: str = typer.Argument(..., help="Schedule id or schedule.name."),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """List the recorded fires for a schedule, newest first.

    Each record carries the flow id verbatim — the same id ``atelier logs
    <id>`` and ``atelier run --resume <id>`` accept — so a failed overnight
    run leads straight to its logs.

    :param ref: schedule id or ``schedule.name`` to inspect.
    :param json_mode: when true, emit machine-readable JSON instead of a table.
    """
    store = _schedule_store()
    job = _resolve_schedule(store, ref)
    if job is None:
        console.print(f"[red]schedule not found:[/red] {ref}")
        raise typer.Exit(code=1)

    records = list(reversed(store.run_history(job.id)))

    if json_mode:
        payload = {
            "id": job.id,
            "name": job.schedule.name,
            "runs": [r.model_dump(mode="json") for r in records],
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    if not records:
        console.print("[yellow]no recorded runs yet[/yellow]")
        return

    table = Table("ran at", "status", "flow_id")
    for r in records:
        status = (
            "[green]ok[/green]"
            if r.status == "succeeded"
            else "[red]FAILED[/red]"
        )
        table.add_row(_format_clock(r.ran_at_iso), status, r.flow_id or "—")
    console.print(table)


@schedule_app.command(
    "run-now",
    help="Run a scheduled conduit immediately (bypasses the daemon).",
)
def schedule_run_now_cmd(
    ref: str = typer.Argument(..., help="Schedule id or schedule.name."),
) -> None:
    """Run a scheduled conduit immediately, bypassing the daemon.

    :param ref: schedule id or ``schedule.name`` to execute now.
    """
    store = _schedule_store()
    job = _resolve_schedule(store, ref)
    if job is None:
        console.print(f"[red]schedule not found:[/red] {ref}")
        raise typer.Exit(code=1)

    working_dir = Path.cwd()
    if job.run_path:
        wd = Path(job.run_path)
        working_dir = wd if wd.is_absolute() else (working_dir / wd).resolve()

    atelier = Atelier(base_dir=working_dir / ".atelier")
    collected_events: list[TaskEvent] = []

    def _on_event(event: TaskEvent) -> None:
        """Collect the task event and render it to the console.

        :param event: the emitted task event to record and display.
        """
        collected_events.append(event)
        render_task_event(event, console)

    captured: dict[str, str | None] = {"id": None}

    def _on_started(fid: str) -> None:
        """Capture the flow id once the run begins.

        :param fid: the flow id assigned when the run starts.
        """
        # The engine also reports each nested tool:conduit run; keep the first.
        if captured["id"] is None:
            captured["id"] = fid

    try:
        flow_id = asyncio.run(
            atelier.run_conduit(
                job.conduit_name,
                dict(job.inputs),
                on_task_event=_on_event,
                on_flow_started=_on_started,
            )
        )
    except Exception as e:  # noqa: BLE001
        render_run_footer(collected_events, console)
        console.print(f"[red]flow failed:[/red] {escape(str(e))}")
        if captured["id"]:
            console.print(f"[red]flow_id:[/red] {captured['id']}")
        raise typer.Exit(code=1)
    render_run_footer(collected_events, console)
    console.print(f"[green]flow_id:[/green] {flow_id}")
