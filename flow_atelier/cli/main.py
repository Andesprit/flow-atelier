"""Root Typer entry point — constructs ``app`` and the three sub-apps,
then imports each command module for its side-effect decorations.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import typer


def _use_utf8_streams() -> None:
    """Re-encode stdout/stderr as UTF-8 on Windows, where they default to cp1252.

    The run stream prints ``▶``, check marks and box-drawing characters, none
    of which the Windows ANSI code page can encode. Without this, printing a
    task banner raises ``UnicodeEncodeError`` and ``atelier run`` dies instead
    of showing its own output. No-op everywhere else, and on any stream that
    is already UTF-8 or is not a real text stream.
    """
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (getattr(stream, "encoding", "") or "").lower() != "utf-8":
            reconfigure(encoding="utf-8", errors="replace")


_use_utf8_streams()

# Sphinx field list (``:param x:``, ``:returns:``, ``:raises:``) and everything
# after it. Command docstrings carry these for the API docs, but click renders
# the whole docstring, so without stripping them `atelier stop --help` shows
# ":param flow_id: flow id ... to halt." to end users.
_DOC_FIELDS_RE = re.compile(r"\n\s*:(?:param|returns?|raises?|rtype|yields?)\b.*", re.DOTALL)


def _help_from_doc(doc: str) -> str:
    """Render a command docstring as CLI help text.

    :param doc: the raw ``__doc__`` of a command handler.
    :returns: the prose portion, with rst literals downgraded to single
        backticks (double backticks render verbatim in a terminal).
    """
    return _DOC_FIELDS_RE.sub("", inspect.cleandoc(doc)).rstrip().replace("``", "`")


class AtelierTyper(typer.Typer):
    """Typer that keeps Sphinx field lists out of ``--help``.

    Derives each command's help text from the prose part of its docstring,
    leaving ``__doc__`` itself intact for doc tooling. An explicit
    ``help=`` on the decorator still wins.
    """

    def command(self, *args, **kwargs):  # type: ignore[override]
        """Register a command, defaulting ``help`` to the docstring prose.

        :returns: the decorator Typer would normally return.
        """
        def decorator(fn):
            """Attach the derived help text, then defer to Typer.

            :param fn: the command handler being registered.
            """
            # Copy rather than mutate: `kwargs` is captured from the enclosing
            # call, so writing to it would leak the first function's help text
            # onto every later one if a decorator object is reused.
            cmd_kwargs = dict(kwargs)
            if cmd_kwargs.get("help") is None and fn.__doc__:
                cmd_kwargs["help"] = _help_from_doc(fn.__doc__)
            return super(AtelierTyper, self).command(*args, **cmd_kwargs)(fn)

        return decorator


app = AtelierTyper(
    help=(
        "Workflows and loops, configured in one YAML file. The steps are shell "
        "commands, AI coding agents, and human approvals."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    """Print the installed version and exit when ``--version`` is passed.

    :param value: ``True`` when the flag is present.
    """
    if value:
        from flow_atelier import __version__

        typer.echo(f"flow-atelier {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the flow-atelier version and exit.",
    ),
) -> None:
    """Root callback hosting global options.

    :param version: eager flag handled by :func:`_version_callback`.
    """
    # Click runs this callback before rendering a subcommand's --help, and
    # an update tip has no place in help output.
    if "--help" in sys.argv:
        return

    from flow_atelier.cli.updater import start_background_update_check

    start_background_update_check()


list_app = AtelierTyper(
    help="List conduits, flows, schedules or harnesses.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(list_app, name="list")

schedule_app = AtelierTyper(
    help="Manage scheduled conduit runs (.atelier/schedules/).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(schedule_app, name="schedule")

harness_app = AtelierTyper(
    help="Check and refresh ACP agents. Installing and logging in stay yours.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(harness_app, name="harness")

# Hidden: kept so scripts written against `atelier scheduler start|status`
# keep working. The commands live under `schedule daemon` and `list schedules`.
scheduler_app = AtelierTyper(no_args_is_help=True, rich_markup_mode="rich")
app.add_typer(scheduler_app, name="scheduler", hidden=True)

# Side-effect imports: each command module decorates its handler against
# the appropriate Typer instance above. Import order is --help order, so
# `list` goes first to keep conduits and flows at the top of its listing.
from flow_atelier.cli.commands import list as _list  # noqa: E402, F401, I001
from flow_atelier.cli.commands import (  # noqa: E402, F401
    add,
    ask,
    check,
    create,
    harness,
    init,
    logs,
    outputs,
    plan,
    rm,
    run,
    schedule,
    scheduler,
    schema,
    self_update,
    serve,
    show,
    status,
    stop,
    timing,
    wait,
)
