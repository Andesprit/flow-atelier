"""`atelier schedule daemon` and `atelier schedule service`, plus hidden `scheduler` aliases."""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import time
from pathlib import Path

import typer

from flow_atelier.cli._shared import _schedule_store, console
from flow_atelier.cli.commands.schedule import schedule_list_cmd
from flow_atelier.cli.main import AtelierTyper, schedule_app, scheduler_app
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.services.scheduler import (
    SchedulerAlreadyRunning,
    SchedulerDaemon,
    ScheduleStore,
    default_local_zone,
)
from flow_atelier.services.scheduler.service import (
    ServiceError,
    ServiceStatus,
    daemon_argv,
    scheduler_service,
    service_environment,
)


@schedule_app.command(
    "daemon",
    help="Run the scheduler daemon in the foreground (Ctrl+C / SIGTERM to stop).",
)
@scheduler_app.command("start", hidden=True)
def schedule_daemon_cmd(
    reload_interval: float = typer.Option(
        30.0, "--reload-interval",
        help="Seconds between schedule store rescans."
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level",
        help="Logging level for the daemon (DEBUG, INFO, WARNING, ERROR)."
    ),
    wait_for_lease: bool = typer.Option(
        False, "--wait-for-lease",
        help="Wait while another scheduler holds the lease, then take over, instead of exiting.",
    ),
    global_atelier_dir: Path | None = typer.Option(
        None, "--global-atelier-dir", hidden=True,
        help="Schedule store to watch. The login service sets it.",
    ),
    log_file: Path | None = typer.Option(
        None, "--log-file", hidden=True,
        help="Append all output to this file. The Windows login service sets it.",
    ),
) -> None:
    """Run the scheduler daemon in the foreground until interrupted.

    :param reload_interval: seconds between schedule store rescans.
    :param log_level: logging level for the daemon (DEBUG/INFO/WARNING/ERROR).
    :param wait_for_lease: retry every ``reload_interval`` seconds while
        another daemon or ``serve`` dispatches jobs, then take over.
    :param global_atelier_dir: schedule store to watch instead of the configured one.
    :param log_file: file that receives stdout and stderr, for consoles nobody sees.
    """
    if global_atelier_dir is not None:
        # Fired runs inherit the store, as they would from the installing shell.
        os.environ["ATELIER_GLOBAL_ATELIER_DIR"] = str(global_atelier_dir)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = log_file.open("a", encoding="utf-8", buffering=1)
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    store = _schedule_store()
    daemon = SchedulerDaemon(
        store,
        default_zone=default_local_zone(),
        default_working_dir=Path.cwd(),
        reload_interval_seconds=reload_interval,
    )
    async def run() -> None:
        waiting = False
        while True:
            try:
                await daemon.start()
                break
            except SchedulerAlreadyRunning as exc:
                if not wait_for_lease:
                    raise
                if not waiting:
                    console.print(f"{exc}; waiting to take over")
                    waiting = True
                await asyncio.sleep(reload_interval)
        console.print(
            f"[green]scheduler running[/green] "
            f"(tz={daemon.default_zone}, reload={reload_interval}s, "
            f"schedules={store.schedules_dir})"
        )
        await daemon.run_forever()

    try:
        asyncio.run(run())
    except SchedulerAlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        pass
    console.print("[dim]scheduler stopped[/dim]")


scheduler_app.command("status", hidden=True)(schedule_list_cmd)


service_app = AtelierTyper(
    help="Start the scheduler at login (launchd, systemd user unit or Task Scheduler).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
schedule_app.add_typer(service_app, name="service")


def _print_service_status(state: ServiceStatus, log_hint: str) -> None:
    """Print where the service is defined, whether it runs and where it logs."""
    if not state.installed:
        typer.echo("Scheduler service: not installed")
        return
    running = "running" if state.running else "not running"
    if state.pid:
        running += f" (pid {state.pid})"
    typer.echo(f"Scheduler service: {running}")
    typer.echo(f"Definition: {state.definition}")
    typer.echo(f"Logs: {log_hint}")


@service_app.command("install")
def service_install_cmd() -> None:
    """Install and start the scheduler service, replacing an earlier install.

    The service records this shell's PATH, so agents found here are found at
    login too. Run it again after you change PATH or move the installation.
    """
    store_dir = AtelierSettings().global_atelier_dir
    try:
        service = scheduler_service()
        argv = daemon_argv(store_dir)
        service.install(argv, service_environment(os.environ), store_dir)
        state = service.status()
    except ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Command: {shlex.join(argv)}")
    typer.echo(f"Schedule store: {store_dir.resolve()}")
    typer.echo(service.install_note)
    _print_service_status(state, service.log_hint(store_dir))


@service_app.command("uninstall")
def service_uninstall_cmd() -> None:
    """Stop the scheduler service and remove it from login items."""
    try:
        removed = scheduler_service().uninstall()
    except ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Scheduler service removed" if removed else "Scheduler service was not installed")
    if pid := _lease_owner(ScheduleStore(AtelierSettings().global_atelier_dir)):
        typer.echo(
            f"A scheduler is still running (pid {pid}). "
            "It may be `atelier serve` or a daemon you started.",
            err=True,
        )


def _lease_owner(store: ScheduleStore, wait: float = 5.0) -> int | None:
    """Return the PID still holding the scheduler lease after ``wait`` seconds, if any."""
    if not store.atelier_dir.is_dir():
        return None
    deadline = time.monotonic() + wait
    while True:
        try:
            store.acquire_daemon_lease().close()
            return None
        except SchedulerAlreadyRunning as exc:
            if time.monotonic() >= deadline:
                return exc.pid
            time.sleep(0.2)


@service_app.command("status")
def service_status_cmd() -> None:
    """Show whether the scheduler service is installed and running."""
    try:
        service = scheduler_service()
        state = service.status()
    except ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _print_service_status(state, service.log_hint(AtelierSettings().global_atelier_dir))
