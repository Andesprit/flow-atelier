"""Local configuration and outbound runner loop."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer

from flow_atelier.cli.main import AtelierTyper, app
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.runner.client import Client
from flow_atelier.services.runner.local import Local
from flow_atelier.services.runner.worker import Worker

runner = AtelierTyper(help="Pair and run an outbound worker.", no_args_is_help=True)
app.add_typer(runner, name="runner")


def _local() -> Local:
    return Local(AtelierSettings().global_atelier_dir)


@runner.command()
def pair(origin: str):
    """Pair with an HTTPS control plane using a single-use invitation."""
    token = typer.prompt("Pairing token", hide_input=True)
    local = _local()
    local.pair(Client(origin, local.root), token)


@runner.command()
def expose(path: Path, label: str):
    """Register a local folder and print its opaque ID."""
    typer.echo(_local().expose(path, label))


@runner.command()
def approve(folder: str, conduit: str, yes: bool = typer.Option(False, "--yes")):
    """Approve the current recipe and its bundled dependencies in one folder."""
    local = _local()

    def review(lines: list[str]) -> None:
        for line in lines:
            typer.echo(line)
        if not yes:
            typer.confirm("Approve this digest?", default=False, abort=True)

    local.approve(folder, conduit, review)
    for warning in local.advertisement_warnings(folder):
        typer.echo(f"Warning: {warning}", err=True)


@runner.command()
def folders():
    """List local folder IDs, labels, paths, and approved conduit digests."""
    for row in _local().folders():
        approval = f"{row['name']} {row['digest'][:12]}" if row["name"] else "(no approvals)"
        typer.echo(f"{row['id']}  {row['label']}  {ascii(row['path'])}  {approval}")


@runner.command()
def revoke(folder: str, conduit: str | None = typer.Argument(None)):
    """Withdraw approval for a conduit or all conduits in a folder."""
    _local().revoke(folder, conduit)


@runner.command()
def unexpose(folder: str):
    """Remove a folder registration and its approvals."""
    _local().unexpose(folder)


@runner.command()
def unpair():
    """Forget this identity, its folder registrations, and its pending records."""
    _local().unpair()


@runner.command()
def status():
    """Show online state and the last acknowledged heartbeat (UTC)."""
    local = _local()
    online, last_ack = local.status()
    when = datetime.fromtimestamp(last_ack, UTC).isoformat() if last_ack is not None else "never"
    state = "online" if online else "offline"
    typer.echo(f"{local.identity()['id']} {state}  last heartbeat {when}")

    with local.db() as db:
        error = db.execute("SELECT last_error FROM runner_state").fetchone()[0]
        if error:
            typer.echo(f"Last error: {error}")
        for row in db.execute("SELECT job,reason FROM jobs WHERE state='refused' "
                              "ORDER BY rowid DESC LIMIT 5"):
            typer.echo(f"{row['job']} refused: {row['reason'] or 'reason unavailable'}")


@runner.command()
def start():
    """Poll for work and publish heartbeats until interrupted."""
    local = _local()
    client = Client(local.identity()["origin"], local.root)
    for warning in local.advertisement_warnings():
        typer.echo(f"Warning: {warning}", err=True)
    try:
        asyncio.run(Worker(local, client).serve())
    except PermissionError:
        raise typer.BadParameter("runner identity revoked or refused") from None
