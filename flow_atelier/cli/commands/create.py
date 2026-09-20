"""`atelier create` command — scaffold a new conduit ready to edit."""
from __future__ import annotations

import typer
import yaml
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import Conduit


def starter_conduit_yaml(name: str, description: str) -> str:
    """Render the one-task starter conduit in the compact shape the README teaches.

    ``atelier init`` and ``atelier create`` both write this, so a new user
    meets one YAML dialect.

    :param name: conduit name, which must match its folder.
    :param description: one-line description.
    :returns: YAML text for ``conduit.yaml``.
    """
    doc = {
        "name": name,
        "description": description,
        "inputs": {"name": "Who to greet"},
        "tasks": [
            {
                "greet": {
                    "description": "greet someone",
                    "task": "echo hello {{inputs.name}}",
                    "tool": "tool:bash",
                    "depends_on": [],
                }
            }
        ],
    }
    return yaml.safe_dump(doc, sort_keys=False)


@app.command("create", help="Scaffold a new conduit ready to edit.")
def create_cmd(
    name: str = typer.Argument(..., help="Name for the new conduit."),
    description: str = typer.Option(
        None, "--description", "-d", help="Conduit description."
    ),
) -> None:
    """Write a minimal valid starter conduit and print how to run it.

    :param name: name of the conduit to create (folder + ``name:`` field).
    :param description: optional description; a generic default is used if omitted.
    """
    text = starter_conduit_yaml(name, description or f"{name} conduit")
    try:
        Conduit.model_validate(yaml.safe_load(text))
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]invalid conduit:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)

    atelier = Atelier()
    try:
        atelier.store.conduit_source(name)
    except FileNotFoundError:
        pass
    else:
        console.print(f"[red]conduit already exists:[/red] {escape(name)}")
        raise typer.Exit(code=1)

    conduit_file = atelier.settings.atelier_dir / "conduits" / name / "conduit.yaml"
    conduit_file.parent.mkdir(parents=True, exist_ok=True)
    conduit_file.write_text(text)

    console.print(
        f"[green]created[/green] conduits/{escape(name)}/conduit.yaml\n"
        f"[dim]→ run it with: atelier run {escape(name)} --input name=world[/dim]"
    )
