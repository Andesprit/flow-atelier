"""Render Typer's command tree without invoking any commands."""

import re
from collections.abc import Iterator
from html import escape
from inspect import unwrap

from rich.markup import escape as escape_markup
from rich.text import Text
from typer import Context
from typer.core import TyperGroup
from typer.main import get_command

from flow_atelier.main import app

INLINE_CODE = re.compile(r"(`[^`\n]+`)")


def plain_help(help_text: str) -> str:
    # Command docstrings may contain square brackets inside inline code.
    protected = INLINE_CODE.sub(lambda match: escape_markup(match[0]), help_text)
    return Text.from_markup(protected, emoji=False).plain


def markdown_help(help_text: str) -> str:
    """Escape literal CLI syntax in prose while keeping inline code intact."""
    return "".join(
        part if index % 2 else escape(part, quote=False).replace("[", r"\[").replace("]", r"\]")
        for index, part in enumerate(INLINE_CODE.split(help_text))
    )


def command_sections(ctx: Context, depth: int) -> Iterator[str]:
    command = ctx.command
    path = ctx.command_path
    rich = getattr(command, "rich_markup_mode", None) == "rich"
    title = "CLI reference" if ctx.parent is None else f"`{path}`"
    yield f"{'#' * depth} {title}\n"
    if command.help:
        help_text = plain_help(command.help) if rich else command.help
        yield markdown_help(help_text) + "\n"

    usage = " ".join([path, *command.collect_usage_pieces(ctx)])
    yield f"```console\n$ {usage}\n```\n"

    params: dict[str, list[str]] = {"argument": [], "option": []}
    for param in command.get_params(ctx):
        record = param.get_help_record(ctx)
        if record is None:
            continue
        name, help_text = record
        source = getattr(param, "help", None)
        if rich and source and help_text.startswith(source):
            # Typer appends plain text such as [required] and [default: …].
            help_text = plain_help(source) + help_text[len(source) :]
        entry = f"- `{name}`"
        if help_text:
            entry += f" — {markdown_help(' '.join(help_text.split()))}"
        params[param.param_type_name].append(entry)

    for kind, entries in params.items():
        if entries:
            yield f"**{kind.title()}s**\n"
            yield "\n".join(entries) + "\n"

    if command.epilog:
        epilog = plain_help(command.epilog) if rich else command.epilog
        yield markdown_help(epilog) + "\n"


def command_contexts(
    ctx: Context, depth: int = 1, hidden: bool = False
) -> Iterator[tuple[Context, int, bool]]:
    command = ctx.command
    hidden = hidden or command.hidden
    yield ctx, depth, hidden
    if isinstance(command, TyperGroup):
        for name in command.list_commands(ctx):
            child = command.get_command(ctx, name)
            assert child is not None
            child_context = child.context_class(child, info_name=name, parent=ctx)
            yield from command_contexts(child_context, depth + 1, hidden)


def render_cli_reference() -> str:
    command = get_command(app)
    root = command.context_class(command, info_name="atelier")
    sections = []
    public_paths = {}
    hidden_paths = {}
    for ctx, depth, hidden in command_contexts(root):
        if not hidden:
            sections.extend(command_sections(ctx, depth))
        callback = ctx.command.callback
        if callback is None or isinstance(ctx.command, TyperGroup):
            continue
        # Typer wraps each registration of the same callback independently.
        if hidden:
            hidden_paths[ctx.command_path] = unwrap(callback)
        else:
            public_paths.setdefault(unwrap(callback), ctx.command_path)

    aliases = [
        f"- `{path}` — same as `{public_paths[callback]}`"
        for path, callback in hidden_paths.items()
        if callback in public_paths
    ]
    if aliases:
        sections.extend(["## Aliases and hidden commands\n", "\n".join(aliases) + "\n"])
    return "\n".join(sections)
