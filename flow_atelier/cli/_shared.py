"""Shared CLI helpers — package-internal.

Nothing outside ``flow_atelier.cli`` should import from this module.
"""
from __future__ import annotations

import difflib
import time
from datetime import datetime

import typer
from rich.console import Console
from rich.markup import escape

from flow_atelier.core.atelier import Atelier
from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.log import LogEntry, TurnUsage
from flow_atelier.schemas.progress import Progress
from flow_atelier.services.scheduler import ScheduleStore

console = Console()

# Monotonic timestamp of the last thing written to the run stream. The
# heartbeat reads it to stay quiet while output is already flowing and
# speak up only during genuine silence (npx cold start, a long tool call,
# a tool:bash task that emits no steps at all).
_last_activity: float = time.monotonic()


def mark_activity() -> None:
    """Record that something was just written to the run stream."""
    global _last_activity
    _last_activity = time.monotonic()


def seconds_since_activity() -> float:
    """Return seconds elapsed since the last :func:`mark_activity` call.

    :returns: seconds of silence on the run stream.
    """
    return time.monotonic() - _last_activity


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    """Parse a list of ``key=value`` strings into a dictionary.

    :param pairs: raw ``--input`` strings collected from the CLI.
    :returns: mapping of input key to value.
    """
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"--input expects key=value, got {p!r}")
        key, value = p.split("=", 1)
        if not key:
            raise typer.BadParameter(f"--input has an empty key: {p!r}")
        if key in out:
            raise typer.BadParameter(f"--input has a duplicate key: {key!r}")
        out[key] = value
    return out


def _exit_unknown_conduit(name: str, known: list[str]) -> None:
    """Print the ``unknown conduit`` error, suggest close names, and exit 1.

    :param name: the conduit name the user typed.
    :param known: every conduit name currently visible to the store.
    """
    console.print(
        f"[red]unknown conduit:[/red] {escape(name)} — try 'atelier list conduits'"
    )
    close = difflib.get_close_matches(name, known, n=3)
    if close:
        console.print(f"[dim]did you mean: {escape(', '.join(close))}?[/dim]")
    raise typer.Exit(code=1)


def _latest_flow_id(atelier: Atelier) -> str | None:
    """Return the id of the most recently started top-level flow.

    Flow ids sort by date but not by time within a day, so the pick is
    made on each flow's ``started_at``; a flow whose progress cannot be
    read sorts first.

    :param atelier: Atelier instance used to enumerate and read flows.
    :returns: the newest flow id, or None when no flows exist.
    """

    def _started(fid: str) -> float:
        try:
            started = _parse_iso(atelier.store.read_progress(fid).started_at)
        except (FileNotFoundError, ValueError):
            started = None
        return started.timestamp() if started else 0.0

    return max(atelier.list_flows(), key=_started, default=None)


def _resolve_flow_id(atelier: Atelier, candidate: str) -> str:
    """Resolve ``candidate`` to a full flow id, supporting git-style prefixes.

    - ``latest`` → the most recently started top-level flow. The resolved id
      is echoed to stderr so stdout pipes and ``--json`` stay clean. Real ids
      start with an eight-digit date, so the word cannot collide with one.
    - Exact top-level id → returned as-is.
    - Exact id of a nested child flow → resolved via the store (``list_flows``
      only enumerates top-level flows, but the store can address children by
      their exact id). Child-id *prefix* matching stays out of scope.
    - Otherwise scans top-level flows. Exactly one prefix match → that id.
    - Zero matches → exits with ``unknown flow`` (code 1).
    - More than one → exits with ``ambiguous flow id`` and lists candidates.

    :param atelier: Atelier instance used to enumerate known flows.
    :param candidate: full flow id or unique prefix supplied by the user.
    :returns: the resolved full flow id.
    """
    if candidate == "latest":
        latest = _latest_flow_id(atelier)
        if latest is None:
            console.print("[red]unknown flow:[/red] latest — no flows recorded yet")
            raise typer.Exit(code=1)
        typer.echo(f"latest: {latest}", err=True)
        return latest
    all_flows = atelier.list_flows()
    if candidate in all_flows:
        return candidate
    try:
        atelier.store.read_progress(candidate)
        return candidate
    except FileNotFoundError:
        pass
    matches = [fid for fid in all_flows if fid.startswith(candidate)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        console.print(
            f"[red]unknown flow:[/red] {escape(candidate)}"
            " — try 'atelier list flows' or 'latest'"
        )
        raise typer.Exit(code=1)
    console.print(f"[red]ambiguous flow id:[/red] {candidate} matches:")
    for m in matches[:10]:
        console.print(f"  - {m}")
    if len(matches) > 10:
        console.print(f"  … and {len(matches) - 10} more")
    raise typer.Exit(code=1)


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a :class:`datetime`.

    :param ts: ISO timestamp (possibly Z-suffixed) or None.
    :returns: parsed datetime, or None when the value is missing/invalid.
    """
    if not ts:
        return None
    try:
        # Engine emits Z-suffixed ISO; fromisoformat handles +00:00 form.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_duration_seconds(seconds: float | None) -> str:
    """Format a duration in seconds as a compact human-readable string.

    :param seconds: duration in seconds, or None for an unknown duration.
    :returns: formatted duration like ``1.2s``, ``5m 04s``, ``2h 03m`` or ``—``.
    """
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _flow_duration_seconds(progress: Progress) -> float | None:
    """Compute the wall-clock duration of a finished flow in seconds.

    :param progress: flow progress containing start and finish timestamps.
    :returns: duration in seconds, or None if the flow has not finished.
    """
    start = _parse_iso(progress.started_at)
    end = _parse_iso(progress.finished_at) if progress.finished_at else None
    if start is None:
        return None
    if end is None:
        # In-flight: don't try to compute against wall-clock here — just omit.
        return None
    return (end - start).total_seconds()


def _format_usage(usage: TurnUsage | None) -> str | None:
    """Format a :class:`TurnUsage` as a compact ``tokens=… cost=…`` line.

    Returns ``None`` when ``usage`` is absent or carries no token count and
    no cost, so callers omit the line entirely rather than printing zeros
    the agent never reported.

    :param usage: the captured usage record, or None.
    :returns: a compact display string, or None when there is nothing to show.
    """
    if usage is None:
        return None
    total = usage.total_tokens
    if total is None and (
        usage.input_tokens is not None or usage.output_tokens is not None
    ):
        total = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    parts: list[str] = []
    if total is not None:
        parts.append(f"tokens={total:,}")
    if usage.cost is not None:
        parts.append(f"cost={usage.cost:g}")
    return "  ".join(parts) if parts else None


def _flow_usage_totals(logs: list[LogEntry]) -> TurnUsage | None:
    """Sum per-step usage across a run's log entries.

    Each harness task runs as its own ACP session and records that session's
    total spend on its :class:`LogEntry`, so the run total is the sum across
    entries (retried iterations are distinct sessions and count separately).
    A token field stays ``None`` when no entry reported it; the whole result
    is ``None`` when no entry carried usage at all.

    :param logs: log entries for the flow (optionally including child flows).
    :returns: a :class:`TurnUsage` of summed fields, or None when none present.
    """
    present = [e.usage for e in logs if e.usage is not None]
    if not present:
        return None
    fields = (
        "input_tokens",
        "output_tokens",
        "cached_read_tokens",
        "cached_write_tokens",
        "thought_tokens",
        "total_tokens",
        "cost",
    )
    summed: dict[str, float | int | None] = {}
    for name in fields:
        values = [getattr(u, name) for u in present if getattr(u, name) is not None]
        summed[name] = sum(values) if values else None
    return TurnUsage(**summed)


def _format_clock(ts: str | None) -> str:
    """Format an ISO timestamp as ``YYYY-MM-DD HH:MM`` in local time.

    :param ts: ISO timestamp string or None.
    :returns: formatted local-time clock string, or ``—`` when unset.
    """
    dt = _parse_iso(ts)
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _format_clock_short(ts: str | None) -> str:
    """Return short-form ``HH:MM`` timestamp for timeline display.

    :param ts: ISO timestamp string or None.
    :returns: formatted ``HH:MM`` local-time string, or ``—`` when unset.
    """
    dt = _parse_iso(ts)
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%H:%M")


def _format_next_fire(value: datetime | None) -> str:
    """Format a scheduler next-fire datetime for display.

    :param value: localized next-fire datetime, or None when not scheduled.
    :returns: formatted ``YYYY-MM-DD HH:MM TZ`` string, or ``—`` when unset.
    """
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _schedule_store() -> ScheduleStore:
    """Build a :class:`ScheduleStore` rooted at the configured atelier dir.

    :returns: a ScheduleStore bound to the current atelier directory.
    """
    settings = AtelierSettings()
    return ScheduleStore(settings.atelier_dir)
