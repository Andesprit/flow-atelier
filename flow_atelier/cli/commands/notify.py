"""Local WhatsApp sender for a run or a final trusted shell step."""
import re

import typer
from pydantic import ValidationError

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.notify import send_summary
from flow_atelier.services.notification.twilio import PHONE, Twilio


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
        recipient = settings.notification_recipient or ""
        if settings.notification_provider != "twilio" or not re.fullmatch(PHONE, recipient):
            raise ValueError("configure notificationProvider (twilio) and an E.164 recipient")
        journal = atelier.store.journal(flow_id)
        if journal is None:
            raise ValueError("notification requires a run journal (migrate legacy runs first)")
        path = settings.global_atelier_dir / "twilio.json"
        try:
            sender = Twilio.model_validate_json(path.read_bytes())
        except ValidationError:
            raise ValueError(f"invalid Twilio settings in {path}") from None
        typer.echo(send_summary(journal, sender, recipient, status, link,
                                retry_uncertain=retry_uncertain))
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None

