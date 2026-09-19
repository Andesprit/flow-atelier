"""`atelier init` command."""
from __future__ import annotations

from pathlib import Path

from flow_atelier.cli._shared import console
from flow_atelier.cli.commands.create import starter_conduit_yaml
from flow_atelier.cli.main import app


@app.command(
    "init",
    help="Scaffold a local .atelier/ directory with a hello-world conduit.",
)
def init_cmd() -> None:
    """Scaffold ``.atelier/`` with a hello-world conduit; idempotent."""
    atelier_dir = Path.cwd() / ".atelier"
    hello_dir = atelier_dir / "conduits" / "hello"
    conduit_file = hello_dir / "conduit.yaml"
    if conduit_file.exists():
        console.print("[yellow]atelier is already set up in this project[/yellow]")
        return
    hello_dir.mkdir(parents=True, exist_ok=True)
    conduit_file.write_text(starter_conduit_yaml("hello", "Say hello"))
    console.print(
        f"[green]initialized[/green] {atelier_dir}\n"
        "try: [bold]atelier run hello --input name=world[/bold]"
    )
