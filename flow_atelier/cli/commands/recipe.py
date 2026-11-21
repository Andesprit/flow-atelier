"""Save one validated project recipe from stdin."""

import sys
from pathlib import Path

import typer

from flow_atelier.cli._shared import cli_error
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import validate_conduit
from flow_atelier.schemas.conduit import Conduit


@app.command("save-recipe")
def save_recipe(name: str):
    """Save a JSON conduit definition from stdin in the current project."""
    try:
        conduit = Conduit.model_validate_json(sys.stdin.read())
        if name != conduit.name:
            raise ValueError("Definition and destination names must match")
        atelier = Atelier()
        path = atelier.settings.atelier_dir.absolute() / "conduits" / name / "conduit.yaml"
        if path.resolve() != path or not path.is_relative_to(Path.cwd().resolve()):
            raise ValueError("Recipe destination must be inside this checkout")
        validate_conduit(conduit)
        atelier.store.write_conduit(conduit)
    except ValueError as exc:
        raise cli_error("invalid_recipe", str(exc)) from exc
    typer.echo(str(path))
