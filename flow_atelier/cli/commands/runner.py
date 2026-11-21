"""Local configuration and outbound runner loop."""

import time
from pathlib import Path

import typer

from flow_atelier.cli.main import AtelierTyper, app
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.runner.client import Client
from flow_atelier.services.runner.local import Local

runner = AtelierTyper(help="Pair and run an outbound worker.", no_args_is_help=True)
app.add_typer(runner, name="runner")


def _local() -> Local:
    return Local(AtelierSettings().global_atelier_dir)


@runner.command()
def pair(origin: str):
    """Pair with an HTTPS control plane using a single-use invitation."""
    token = typer.prompt("Pairing token", hide_input=True)
    _local().pair(Client(origin), token)


@runner.command()
def expose(path: Path, label: str):
    """Register a local folder and print its opaque ID."""
    typer.echo(_local().expose(path, label))


@runner.command()
def approve(folder: str, conduit: str):
    """Approve the current recipe and its bundled dependencies in one folder."""
    _local().approve(folder, conduit)


@runner.command()
def start():
    """Poll for work and publish heartbeats until interrupted."""
    local = _local()
    client = Client(local.identity()["origin"])
    while True:
        try:
            local.poll(client, claim=False)
        except PermissionError:
            raise typer.BadParameter("runner identity revoked or refused") from None
        except (ConnectionError, TimeoutError):
            pass
        time.sleep(10)
