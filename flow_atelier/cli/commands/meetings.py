"""Trusted meeting intake step of the platform conduit."""
import asyncio
import json
from pathlib import Path

import typer

from flow_atelier.cli.main import app
from flow_atelier.core.settings import AtelierSettings


@app.command("meetings-intake")
def meetings_intake_cmd() -> None:
    """Read completed Dictaria meetings and enqueue the configured user's tasks."""
    from flow_atelier.modules.meetings import PollAlreadyActive, intake
    from flow_atelier.services.store.claims import ClaimQueueFull

    try:
        typer.echo(json.dumps(asyncio.run(intake(AtelierSettings(), Path.cwd()))))
    except Exception as exc:
        # AnyIO nests exceptions leaving its transport and session task groups.
        while isinstance(exc, ExceptionGroup):
            exc = exc.exceptions[0]
        if isinstance(exc, ClaimQueueFull):
            message = "meeting queue full; retry after queued work is consumed"
        elif type(exc) in (ValueError, PollAlreadyActive):
            message = str(exc)
        else:
            message = f"meeting intake failed ({type(exc).__name__}); check MCP and Claude login"
        typer.echo(message, err=True)
        raise typer.Exit(1) from None
