"""`atelier rm` and `atelier prune` commands — flow-run retention."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import typer

from flow_atelier.cli._shared import _resolve_flow_id, console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.flow import parse_flow_id
from flow_atelier.schemas.progress import FlowStatus


def _is_running(atelier: Atelier, flow_id: str) -> bool:
    """Return whether ``flow_id`` is in the non-terminal ``running`` state.

    Read errors are treated as running so an in-flight run is never reaped.

    :param atelier: Atelier instance used to read progress.
    :param flow_id: flow identifier.
    :returns: True if the flow is running or its status can't be read.
    """
    try:
        return atelier.get_status(flow_id).status == FlowStatus.running
    except Exception:  # noqa: BLE001 — unreadable progress → treat as in-flight
        return True


@app.command("rm")
def rm_cmd(
    flow_id: str = typer.Argument(..., help="Flow id to delete (unique prefix or 'latest' ok)."),
    force: bool = typer.Option(
        False, "--force", help="Delete even if the flow is still running."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of text."
    ),
) -> None:
    """Delete a single flow run directory.

    :param flow_id: flow id (or unique prefix) to delete.
    :param force: delete even when the flow's status is ``running``.
    :param yes: skip the interactive confirmation prompt.
    :param json_mode: when true, emit machine-readable JSON instead of text.
    """
    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)
    try:
        progress = atelier.get_status(flow_id)
    except FileNotFoundError:
        console.print(f"[red]unknown flow:[/red] {flow_id}")
        raise typer.Exit(code=1)

    if progress.status == FlowStatus.running and not force:
        console.print(
            f"[red]flow is still running:[/red] {flow_id} "
            "(pass --force to delete anyway)"
        )
        raise typer.Exit(code=1)

    if not yes and not typer.confirm(f"delete flow {flow_id}?"):
        console.print("aborted")
        raise typer.Exit(code=1)

    deleted = atelier.delete_flow(flow_id)
    if json_mode:
        typer.echo(json.dumps({"flow_id": flow_id, "deleted": deleted}))
        return
    console.print(f"deleted flow {flow_id}" if deleted else f"nothing to delete: {flow_id}")


@app.command("prune")
def prune_cmd(
    conduit: str | None = typer.Option(
        None, "--conduit", "-c", help="Only prune flows for this conduit."
    ),
    older_than: int | None = typer.Option(
        None, "--older-than", help="Prune flows whose id date is older than N days."
    ),
    keep: int | None = typer.Option(
        None, "--keep", help="Keep the N most-recent flows; prune the rest."
    ),
    force: bool = typer.Option(
        False, "--force", help="Include running flows (off by default)."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of text."
    ),
) -> None:
    """Bulk-delete terminal flow runs by age and/or keep-count.

    :param conduit: restrict candidates to this conduit.
    :param older_than: prune flows older than this many days (by id date prefix).
    :param keep: retain the N most-recent flows, prune the older remainder.
    :param force: include ``running`` flows instead of skipping them.
    :param yes: skip the interactive confirmation prompt.
    :param json_mode: when true, emit machine-readable JSON instead of text.
    """
    if older_than is None and keep is None:
        console.print(
            "[red]refusing to prune:[/red] pass --older-than and/or --keep "
            "to select which flows to delete."
        )
        raise typer.Exit(code=1)

    atelier = Atelier()
    candidates = atelier.list_flows(conduit)  # sorted ascending (oldest first)

    selected = set(candidates)
    if older_than is not None:
        cutoff = datetime.now(UTC).date() - timedelta(days=older_than)
        older: set[str] = set()
        for fid in candidates:
            try:
                _, _, date = parse_flow_id(fid)
                fid_date = datetime.strptime(date, "%Y%m%d").date()
            except ValueError:
                continue
            if fid_date < cutoff:
                older.add(fid)
        selected &= older
    if keep is not None:
        retained = set(candidates[-keep:]) if keep > 0 else set()
        selected -= retained

    if not force:
        selected = {fid for fid in selected if not _is_running(atelier, fid)}

    to_delete = sorted(selected)
    if not to_delete:
        if json_mode:
            typer.echo(json.dumps({"deleted": []}))
        else:
            console.print("nothing to prune")
        return

    # JSON mode is non-interactive: it cannot prompt, so it requires --yes to
    # delete. Without --yes it returns a dry-run preview and deletes nothing.
    if json_mode and not yes:
        typer.echo(json.dumps({"would_delete": to_delete, "deleted": []}))
        return

    if not json_mode:
        console.print("will delete:")
        for fid in to_delete:
            console.print(f"  - {fid}")
        if not yes and not typer.confirm(f"delete {len(to_delete)} flow(s)?"):
            console.print("aborted")
            raise typer.Exit(code=1)

    deleted = [fid for fid in to_delete if atelier.delete_flow(fid)]
    if json_mode:
        typer.echo(json.dumps({"deleted": deleted}))
    else:
        console.print(f"deleted {len(deleted)} flow(s)")
