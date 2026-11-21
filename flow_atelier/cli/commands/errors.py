"""Manage durable claims for signed error reports."""
import typer

from flow_atelier.cli.main import errors_app
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.store.claims import EventClaims


@errors_app.command("reopen")
def reopen_cmd(run_id: str) -> None:
    """Allow the next report for this terminal error run to start a new run."""
    claims = EventClaims(AtelierSettings().global_atelier_dir, "errors")
    if not claims.reopen(run_id):
        raise typer.BadParameter("Expected the run ID of a terminal error claim")
    typer.echo(f"Reopened {run_id}. The next matching report will start a new run.")
