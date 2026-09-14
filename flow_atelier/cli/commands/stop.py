"""`atelier stop` command.

Gracefully halt a healthy, locally-running flow by signalling its runner
process, instead of hunting for a PID and ``kill``-ing it by hand. The command
is conservative: it refuses (and does nothing) for any flow that already
finished, whose runner died, that runs on another host, or that shares its
runner process with other flows. SIGTERM is coarse — it signals the whole
process — so it is only sent when that process is known to own exactly one
running flow.

Note on PID recycling: between a flow starting and this command running the OS
could reuse ``runner_pid`` for an unrelated process. The host + alive + running
checks shrink that window but cannot fully close it.
"""
from __future__ import annotations

import os
import signal
import time

import typer

from flow_atelier.cli._shared import _resolve_flow_id, console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import StopDecision, stop_decision

_POLL_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.25


def _other_running_progresses(atelier: Atelier, flow_id: str) -> list:
    """Read progress for every known flow except ``flow_id``.

    :param atelier: Atelier instance used to enumerate and read flows.
    :param flow_id: the target flow to exclude.
    :returns: progress snapshots of all other flows (unreadable ones skipped).
    """
    others = []
    for fid in atelier.list_flows():
        if fid == flow_id:
            continue
        try:
            others.append(atelier.store.read_progress(fid))
        except (FileNotFoundError, ValueError):
            continue
    return others


@app.command("stop")
def stop_cmd(
    flow_id: str = typer.Argument(..., help="Flow id to stop (unique prefix or 'latest' ok)."),
) -> None:
    """Gracefully stop a healthy, locally-running flow.

    :param flow_id: flow id (or unique prefix) of the running flow to halt.
    """
    atelier = Atelier()
    flow_id = _resolve_flow_id(atelier, flow_id)
    try:
        progress = atelier.get_status(flow_id)
    except FileNotFoundError:
        console.print(f"[red]unknown flow:[/red] {flow_id}")
        raise typer.Exit(code=1)

    others = _other_running_progresses(atelier, flow_id)
    decision = stop_decision(progress, others)

    if decision is not StopDecision.stoppable:
        messages = {
            StopDecision.not_running: f"flow already {progress.status.value}",
            StopDecision.crashed: "runner process is gone; nothing to stop",
            StopDecision.foreign_host: (
                f"running on host {progress.runner_host}; cannot stop from here"
            ),
            StopDecision.no_pid: "no runner recorded; cannot stop",
            StopDecision.not_stoppable: (
                "runs inside a scheduler/server process; cannot stop individually"
            ),
            StopDecision.shared_runner: (
                "runs inside a shared scheduler process; cannot stop individually"
            ),
        }
        console.print(f"[yellow]cannot stop {flow_id}:[/yellow] {messages[decision]}")
        raise typer.Exit(code=1)

    assert progress.runner_pid is not None  # guaranteed by stop_decision
    try:
        os.kill(progress.runner_pid, signal.SIGTERM)
    except ProcessLookupError:
        # Raced with the runner exiting between the decision and the signal.
        console.print(f"[yellow]cannot stop {flow_id}:[/yellow] runner just exited")
        raise typer.Exit(code=1)

    console.print(f"[dim]stop signal sent to {flow_id}; waiting for shutdown…[/dim]")
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            current = atelier.get_status(flow_id)
        except (FileNotFoundError, ValueError):
            continue
        if current.status.value != "running":
            colour = "green" if current.status.value == "stopped" else "yellow"
            console.print(
                f"[{colour}]flow {flow_id} {current.status.value}[/{colour}]"
            )
            return

    console.print(
        f"[yellow]stop signal sent; flow {flow_id} still shutting down[/yellow]"
    )
