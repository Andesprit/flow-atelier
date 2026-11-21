"""Local read-only history for dashboard chat."""
import json

import typer

from flow_atelier.cli._shared import cli_error
from flow_atelier.cli.main import app
from flow_atelier.services.history import read_history


@app.command("history")
def history_cmd(
    source_id: str | None = typer.Argument(None, help="Read the transcript with this source ID."),
    json_mode: bool = typer.Option(False, "--json", help="Emit session data as JSON."),
) -> None:
    """List local agent session sources, or read one transcript by ID."""
    try:
        typer.echo(json.dumps(read_history(source_id), ensure_ascii=False, indent=2))
    except ValueError as exc:
        raise cli_error("unknown_session", str(exc), json_mode=json_mode) from exc
