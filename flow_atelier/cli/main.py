"""Root Typer entry point — constructs ``app`` and the three sub-apps,
then imports each command module for its side-effect decorations.
"""
from __future__ import annotations

import inspect
import os
import re
import sys
from contextlib import nullcontext

import typer
import yaml
from typer.core import TyperCommand, TyperGroup

from flow_atelier.cli._shared import cli_error, json_diagnostics

# Recent Typer versions raise usage errors from their bundled copy of Click.
try:
    from typer._click.exceptions import BadOptionUsage, NoSuchOption, UsageError
except ImportError:
    from click.exceptions import BadOptionUsage, NoSuchOption, UsageError


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


def _usage_error(exc: UsageError) -> typer.Exit:
    """Include the offending parameter and its choices when Click knows them."""
    details: dict[str, object] = {}
    if isinstance(exc, typer.BadParameter):
        if exc.param is not None:
            details["parameter"] = exc.param.opts[0]
            descriptor = getattr(exc.param, "type_descriptor", None) or getattr(
                exc.param, "type", None
            )
            if (choices := getattr(descriptor, "choices", None)) is not None:
                details["allowed"] = list(choices)
        elif exc.param_hint:
            details["parameter"] = exc.param_hint
    elif isinstance(exc, (BadOptionUsage, NoSuchOption)):
        details["parameter"] = exc.option_name
    return cli_error(
        "invalid_arguments", exc.format_message(), exit_code=exc.exit_code, json_mode=True,
        **details,
    )


class AtelierGroup(TyperGroup):
    """Report inaccessible paths for leaf commands and group defaults alike."""

    def invoke(self, ctx: typer.Context) -> object:
        try:
            return super().invoke(ctx)
        except (PermissionError, NotADirectoryError) as exc:
            raise cli_error("filesystem_error", str(exc)) from exc


class AtelierCommand(TyperCommand):
    """Format usage errors on both sides of Click's argument parser."""

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if "--json" not in args and "--jsonl" not in args:
            return super().parse_args(ctx, args)
        try:
            return super().parse_args(ctx, args.copy())
        except UsageError as exc:
            # Ask Click whether the format was a flag, an option value or a
            # positional after "--", even if a preceding option was invalid.
            probe = typer.Context(self, resilient_parsing=True, ignore_unknown_options=True)
            options, _, _ = self.make_parser(probe).parse_args(args)
            if options.get("json_mode") or options.get("jsonl"):
                raise _usage_error(exc) from exc
            raise

    def invoke(self, ctx: typer.Context) -> object:
        machine = ctx.params.get("json_mode") or ctx.params.get("jsonl")
        streaming = self.name == "run" or ctx.params.get("follow")
        with json_diagnostics() if machine and not streaming else nullcontext():
            try:
                return super().invoke(ctx)
            except UsageError as exc:
                if machine:
                    raise _usage_error(exc) from exc
                raise
            except (OSError, ValueError, yaml.YAMLError) as exc:
                if machine:
                    code = (
                        "filesystem_error" if isinstance(exc, (PermissionError, NotADirectoryError))
                        else "command_failed"
                    )
                    raise cli_error(code, str(exc), json_mode=True) from exc
                raise


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
            cmd_kwargs.setdefault("cls", AtelierCommand)
            if cmd_kwargs.get("help") is None and fn.__doc__:
                cmd_kwargs["help"] = _help_from_doc(fn.__doc__)
            return super(AtelierTyper, self).command(*args, **cmd_kwargs)(fn)

        return decorator


app = AtelierTyper(
    cls=AtelierGroup,
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
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the flow-atelier version and exit.",
    ),
    require_read_only_stores: bool = typer.Option(
        False, "--require-read-only-stores", hidden=True,
        help="Require a CLI whose check and show can inspect missing stores without writing.",
    ),
) -> None:
    """Root callback hosting global options.

    :param version: eager flag handled by :func:`_version_callback`.
    """
    # Older CLIs reject this option during parsing, before opening any store.
    if require_read_only_stores and ctx.invoked_subcommand not in {"check", "show"}:
        raise typer.BadParameter("--require-read-only-stores applies to check and show")
    # The updater writes a throttle stamp. Run it only for mutating commands.
    if ctx.invoked_subcommand not in {
        "add", "ask", "compose", "create", "init", "install", "remove", "rm", "run",
        "serve", "stop", "uninstall", "update",
    } or "--help" in sys.argv or "--json" in sys.argv:
        return

    from flow_atelier.cli.updater import start_background_update_check

    start_background_update_check()


list_app = AtelierTyper(
    help="List conduits, flows, schedules or harnesses.",
    invoke_without_command=True,
    rich_markup_mode="rich",
)
app.add_typer(list_app, name="list")

schedule_app = AtelierTyper(
    help="Manage scheduled conduit runs (.atelier/schedules/).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(schedule_app, name="schedule")

errors_app = AtelierTyper(help="Manage incoming error reports.", no_args_is_help=True)
app.add_typer(errors_app, name="errors")

migrate_app = AtelierTyper(help="Migrate legacy project records.", no_args_is_help=True)
app.add_typer(migrate_app, name="migrate")

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
    compare,
    compose,
    create,
    diagnose,
    errors,
    experiment,
    harness,
    init,
    logs,
    meetings,
    migrate,
    notify,
    outputs,
    plan,
    rm,
    run,
    runner,
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
