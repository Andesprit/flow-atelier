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
    from flow_atelier.modules.meetings import poll
    from flow_atelier.services.dictaria.client import connect
    from flow_atelier.services.store.claims import ClaimQueueFull

    async def run() -> dict:
        settings = AtelierSettings()
        account = (settings.dictaria_account or "").strip()
        if not account or not settings.dictaria_url:
            raise ValueError("configure dictariaAccount and dictariaUrl in user settings")
        async with connect(settings.dictaria_url, account) as reader:
            return await poll(reader, settings.global_atelier_dir, account,
                              settings.dictaria_aliases, Path.cwd())

    try:
        typer.echo(json.dumps(asyncio.run(run())))
    except Exception as exc:
        # AnyIO nests exceptions leaving its transport and session task groups.
        while isinstance(exc, ExceptionGroup):
            exc = exc.exceptions[0]
        if isinstance(exc, ClaimQueueFull):
            message = "meeting queue full; retry after queued work is consumed"
        elif type(exc) is ValueError:
            message = str(exc)
        else:
            message = f"meeting intake failed ({type(exc).__name__}); check MCP and Claude login"
        typer.echo(message, err=True)
        raise typer.Exit(1) from None
