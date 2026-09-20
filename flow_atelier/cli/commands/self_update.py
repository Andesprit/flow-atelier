"""`atelier self-update` command — replace the frozen binary with the latest release."""
from __future__ import annotations

import typer
from rich.markup import escape

from flow_atelier.cli._shared import console
from flow_atelier.cli.main import app
from flow_atelier.cli.updater import UpdateError, is_frozen_binary, self_update


@app.command("self-update")
def self_update_cmd() -> None:
    """Download, verify and install the latest release of the atelier binary."""
    if not is_frozen_binary():
        console.print(
            "[yellow]this install is managed by pip/uv[/yellow] — "
            "run `uv tool upgrade flow-atelier` instead"
        )
        raise typer.Exit(code=1)

    from flow_atelier import __version__

    console.print(f"[dim]checking for a release newer than {__version__}…[/dim]")
    try:
        tag = self_update()
    except (UpdateError, OSError) as exc:
        console.print(f"[red]update failed:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)
    if tag is None:
        console.print(f"[green]already up to date[/green] ({__version__})")
        return
    console.print(f"[green]updated[/green] flow-atelier {__version__} → {tag.lstrip('v')}")
