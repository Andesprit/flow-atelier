"""Local WhatsApp sender for a run or a final trusted shell step."""
import typer

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.notify import notify_configured


@app.command("notify")
def notify_cmd(
    flow_id: str = typer.Argument(..., help="Exact run id."),
    status: str | None = typer.Option(None, help="Short summary, defaults to the run status."),
    link: list[str] = typer.Option([], help="Draft GitHub PR URL (repeatable)."),
    retry_uncertain: bool = typer.Option(False, help="Retry after checking provider history."),
) -> None:
    """Send the run's WhatsApp summary to the configured recipient."""
    atelier = Atelier()
    settings = atelier.settings
    try:
        journal = atelier.store.journal(flow_id)
        if journal is None:
            raise ValueError("notification requires a run journal (migrate legacy runs first)")
        result = notify_configured(settings, journal, status, link, retry_uncertain=retry_uncertain)
        typer.echo(result)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None

