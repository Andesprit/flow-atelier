"""`atelier serve` command."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import typer

from flow_atelier.cli._shared import console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.api import ScheduledJob
from flow_atelier.schemas.log import TaskEvent
from flow_atelier.services.api.app import LOOPBACK_HOSTS, FastApiServer
from flow_atelier.services.api.scheduler_bus import SchedulerEventBus
from flow_atelier.services.scheduler import SchedulerDaemon, default_local_zone
from flow_atelier.services.scheduler.store import ScheduleStore


@app.command(
    "serve",
    help="Run the FastAPI HTTP + WebSocket server with the embedded scheduler.",
)
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(
        8000, "--port", help="Bind port (use 0 for an ephemeral port)."
    ),
    reload_interval: float = typer.Option(
        30.0, "--reload-interval",
        help="Seconds between schedule store rescans."
    ),
    cors_origin: list[str] = typer.Option(
        [], "--cors-origin",
        help="Allowed CORS origin (repeatable). Default = localhost only."
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level",
        help="Logging level for the server."
    ),
) -> None:
    """Run the FastAPI HTTP and WebSocket server with the embedded scheduler.

    :param host: bind host for the HTTP server.
    :param port: bind port (use 0 for an ephemeral port).
    :param reload_interval: seconds between schedule store rescans.
    :param cors_origin: allowed CORS origins (defaults to localhost-only
        origins when empty).
    :param log_level: logging level for uvicorn and the daemon.
    """
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )

    settings = AtelierSettings()
    global_schedule_store = ScheduleStore(settings.global_atelier_dir)

    # Conduits and flows resolve exactly as they do for the CLI: project store
    # (./.atelier) first, global store (~/.atelier) as fallback. Rooting the
    # facade at the global dir instead would hide every conduit `atelier init`
    # creates from the UI.
    atelier = Atelier(settings=settings)
    # Schedules are the one global resource — a single daemon spans projects —
    # so the API writes to the same store the daemon below watches.
    atelier.schedule_store = global_schedule_store
    bus = SchedulerEventBus()
    atelier.scheduler_bus = bus  # type: ignore[attr-defined]

    async def _broadcasting_executor(
        job: ScheduledJob, working_dir: Path, report: Callable[[str], None]
    ) -> None:
        """Run the conduit and fan lifecycle envelopes out to the bus.

        :param report: daemon's flow-id reporter, fed the captured id so the
            scheduler can record which run this fire produced.
        """
        base = {
            "schedule_id": job.id,
            "schedule_name": job.schedule.name,
            "conduit_name": job.conduit_name,
            "run_path": str(working_dir),
        }
        # Root the run at the job's own run_path, matching the daemon's default
        # fire action (services/scheduler/runner.py) so a scheduled conduit
        # resolves against the project it was scheduled for.
        scheduled_atelier = Atelier(base_dir=working_dir / ".atelier")
        captured: dict[str, str | None] = {"flow_id": None}
        # Strong references for fire-and-forget broadcasts — the loop keeps
        # only a weak reference to a bare create_task, which can be GC'd
        # mid-flight and drop the envelope.
        pending: set[asyncio.Task] = set()

        def _spawn(coro) -> None:
            task = asyncio.create_task(coro)
            pending.add(task)
            task.add_done_callback(pending.discard)

        def _on_started(flow_id: str) -> None:
            # The engine also reports each nested tool:conduit run; keep the first.
            if captured["flow_id"] is not None:
                return
            captured["flow_id"] = flow_id
            report(flow_id)
            _spawn(
                bus.broadcast(
                    {"type": "scheduled_run_started", "flow_id": flow_id, **base}
                )
            )

        def _on_task_event(event: TaskEvent) -> None:
            _spawn(
                bus.broadcast(
                    {
                        "type": "scheduled_task_event",
                        "flow_id": captured["flow_id"],
                        "event": event.model_dump(mode="json"),
                        **base,
                    }
                )
            )

        try:
            flow_id = await scheduled_atelier.run_conduit(
                job.conduit_name,
                dict(job.inputs),
                on_flow_started=_on_started,
                on_task_event=_on_task_event,
                working_dir=working_dir,
            )
        except Exception as e:  # noqa: BLE001
            await bus.broadcast(
                {
                    "type": "scheduled_run_failed",
                    "flow_id": captured["flow_id"],
                    "error": str(e),
                    **base,
                }
            )
            raise
        await bus.broadcast(
            {"type": "scheduled_run_complete", "flow_id": flow_id, **base}
        )

    daemon = SchedulerDaemon(
        global_schedule_store,
        executor=_broadcasting_executor,
        default_zone=default_local_zone(),
        default_working_dir=Path.cwd(),
        reload_interval_seconds=reload_interval,
    )
    # The schedules POST/DELETE handlers look here to opportunistically
    # re-sync the daemon when a schedule is created or removed.
    atelier.scheduler_daemon = daemon  # type: ignore[attr-defined]

    @asynccontextmanager
    async def _lifespan(app):
        """FastAPI lifespan context that starts and stops the scheduler daemon.

        :param app: the FastAPI application receiving the lifespan event.
        """
        await daemon.start()
        try:
            yield
        finally:
            await daemon.stop()

    # Refuse rather than warn. This API runs shell commands, so an
    # unauthenticated bind that anything but loopback can reach is a remote
    # code execution surface, and `api_token` now defaults to empty — a
    # printed warning scrolls past in the same second the port opens.
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.api_token:
        console.print(
            f"[red]error:[/red] refusing to serve on non-loopback host {host!r} "
            "without ATELIER_API_TOKEN. Anyone who can reach this address could "
            "run shell commands via the API. Set ATELIER_API_TOKEN, or bind "
            "127.0.0.1 (the default)."
        )
        raise typer.Exit(1)

    # Pin the accepted Host header so a rebound DNS name cannot drive this API
    # (see LOOPBACK_HOSTS). A wildcard bind can be reached under any number of
    # names we cannot enumerate here, so it accepts all of them — safe only
    # because the check above has already established that a wildcard bind
    # carries a token.
    wildcard_bind = host in ("0.0.0.0", "::", "")
    allowed_hosts = ["*"] if wildcard_bind else sorted({host, *LOOPBACK_HOSTS})

    cors = list(cors_origin) if cors_origin else None
    api_app = FastApiServer().create_app(
        atelier,
        cors_origins=cors,
        api_token=settings.api_token or None,
        allowed_hosts=allowed_hosts,
    )
    api_app.router.lifespan_context = _lifespan

    config = uvicorn.Config(
        api_app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        lifespan="on",
    )
    server = uvicorn.Server(config)

    async def _run() -> None:
        """Start uvicorn and print the actual bind address once it is ready."""
        serve_task = asyncio.create_task(server.serve())
        # Wait for uvicorn to bind so we can print the actual port.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while not server.started and loop.time() < deadline:
            await asyncio.sleep(0.05)
        actual = port
        try:
            if server.servers:
                actual = server.servers[0].sockets[0].getsockname()[1]
        except (AttributeError, IndexError, OSError):
            pass
        console.print(
            f"[green]atelier serve[/green] running at "
            f"[bold]http://{host}:{actual}[/bold]"
        )
        await serve_task

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
