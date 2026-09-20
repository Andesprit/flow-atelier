"""`atelier wait` command — block until an already-started flow finishes.

Another terminal, the dashboard or the scheduler starts a run; this command
joins it, watches the progress file that runner owns, and turns the outcome
into a shell verdict: exit 0 only for a persisted ``completed``, 1 for a run
that ended badly, 124 when the budget expired while it was still going. On
success stdout carries nothing but the resolved flow id, so the next command
can address that exact run instead of resolving ``latest`` a second time and
landing on newer, unrelated work::

    flow_id=$(atelier wait latest --timeout 60) && atelier outputs "$flow_id" --json

It is a pure observer: it never starts, stops, signals or resumes anything.
Giving up here — on timeout or Ctrl-C — ends the watching, not the run.
"""
from __future__ import annotations

import time

import typer
from rich.markup import escape

from flow_atelier.cli._shared import _resolve_flow_id, err_console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.liveness import is_crashed
from flow_atelier.schemas.progress import FlowStatus, Progress

# Same cadence as the `logs --follow` tailer: fast enough to feel immediate,
# cheap because each poll reads one small progress file and nothing else.
_POLL_INTERVAL_SECONDS = 0.25
# Convention shared with `timeout(1)`: the work did not fail, the wait did.
_TIMEOUT_EXIT_CODE = 124


def _fail(message: str) -> typer.Exit:
    """Report an unusable flow on stderr and return the exit to raise.

    :param message: plain-text diagnostic; Rich markup in it is escaped.
    :returns: the ``typer.Exit(1)`` the caller raises.
    """
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=1)


def _read_progress(atelier: Atelier, flow_id: str) -> Progress:
    """Return the snapshot the runner last persisted, or exit 1 saying why not.

    A run can be deleted, or its ``progress.json`` truncated, while we watch
    it. Either way the wait has nothing left to observe, so it says so in one
    line rather than raising a traceback or falling through to success.

    :param atelier: Atelier instance rooted at the caller's store.
    :param flow_id: the resolved full flow id being observed.
    :returns: the parsed progress snapshot.
    """
    try:
        return atelier.store.read_progress(flow_id)
    except FileNotFoundError:
        raise _fail(f"unknown flow: {flow_id}")
    except OSError as exc:
        raise _fail(f"cannot read progress for {flow_id}: {exc.strerror or exc}")
    except ValueError:
        # Pydantic's ValidationError included: a half-written or hand-edited
        # progress.json is unreadable, not a verdict.
        raise _fail(f"invalid progress record for {flow_id}")


def _verdict(flow_id: str, progress: Progress) -> None:
    """Exit with this snapshot's verdict, or return when it is still running.

    :param flow_id: the resolved full flow id, printed on success.
    :param progress: the snapshot to judge.
    """
    if progress.status is FlowStatus.running:
        return
    if progress.status is FlowStatus.completed:
        typer.echo(flow_id)
        raise typer.Exit(code=0)
    raise _unsuccessful(flow_id, progress.status.value)


def _unsuccessful(flow_id: str, state: str) -> typer.Exit:
    """Name the observed end state on stderr and return the exit to raise.

    :param flow_id: the resolved full flow id.
    :param state: the observed state word (``failed``/``stopped``/``crashed``).
    :returns: the ``typer.Exit(1)`` the caller raises.
    """
    err_console.print(f"[yellow]flow {flow_id} {state}[/yellow]")
    err_console.print(f"[dim]→ atelier status {flow_id}[/dim]")
    err_console.print(f"[dim]→ atelier logs {flow_id}[/dim]")
    if state == "crashed":
        err_console.print(f"[dim]→ atelier run --resume {flow_id}[/dim]")
    return typer.Exit(code=1)


@app.command("wait")
def wait_cmd(
    flow_id: str = typer.Argument(
        ..., help="Flow id to wait for (unique prefix or 'latest' ok)."
    ),
    timeout: int = typer.Option(
        60, "--timeout", help="Seconds to keep watching before giving up."
    ),
) -> None:
    """Wait for an already-started flow to finish, then print its id.

    Exits 0 only when the run saved `completed`, with the resolved flow id
    and nothing else on stdout, so a script can read back that exact run:
    `flow_id=$(atelier wait latest --timeout 60) && atelier outputs "$flow_id"`.
    Exits 1 when it failed, was stopped, or its runner died; 124 when the
    timeout expired while it was still running; 130 on Ctrl-C. Waiting starts
    nothing and changes nothing — giving up leaves the run going.

    :param flow_id: flow id (or unique prefix) of the run to observe.
    :param timeout: whole seconds to keep watching before exiting 124.
    """
    if timeout <= 0:
        raise typer.BadParameter("--timeout must be a positive number of seconds")

    atelier = Atelier()
    try:
        flow_id = _resolve_flow_id(atelier, flow_id)
    except (OSError, ValueError) as exc:
        # `latest` and the exact-child lookup both read saved flows, so an
        # unreadable record can break resolution before any waiting starts.
        raise _fail(f"cannot resolve a flow to wait for: {exc}")

    deadline = time.monotonic() + timeout
    announced = False
    try:
        while True:
            progress = _read_progress(atelier, flow_id)
            _verdict(flow_id, progress)
            if is_crashed(progress):
                # The liveness probe is not instantaneous: the runner may have
                # published its result and exited between the read above and
                # the probe, which reads as "running with a dead pid". Re-read
                # before accusing it — and re-probe, because `run --resume` may
                # have handed the flow to a new, live runner.
                progress = _read_progress(atelier, flow_id)
                _verdict(flow_id, progress)
                if is_crashed(progress):
                    raise _unsuccessful(flow_id, "crashed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not announced:
                err_console.print(
                    f"[dim]waiting for {flow_id} (up to {timeout}s)…[/dim]"
                )
                announced = True
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
    except KeyboardInterrupt:
        err_console.print("[dim]— wait interrupted; the flow keeps running —[/dim]")
        raise typer.Exit(code=130)

    err_console.print(
        f"[yellow]timed out after {timeout}s;[/yellow] "
        f"flow {flow_id} is still running"
    )
    err_console.print(f"[dim]→ atelier status {flow_id}[/dim]")
    raise typer.Exit(code=_TIMEOUT_EXIT_CODE)
