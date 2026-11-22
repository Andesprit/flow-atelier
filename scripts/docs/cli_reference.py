"""Render Typer's command tree without invoking any commands."""

import re
from collections.abc import Iterator
from html import escape

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


def command_sections(ctx: Context, depth: int = 1) -> Iterator[str]:
    command = ctx.command
    path = ctx.command_path
    rich = getattr(command, "rich_markup_mode", None) == "rich"
    title = "CLI reference" if ctx.parent is None else f"`{path}`"
    yield f"{'#' * depth} {title}\n"
    if command.hidden:
        yield "**Hidden command**\n"
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

    if isinstance(command, TyperGroup):
        for name in command.list_commands(ctx):
            child = command.get_command(ctx, name)
            assert child is not None
            child_context = child.context_class(child, info_name=name, parent=ctx)
            yield from command_sections(child_context, depth + 1)


def render_cli_reference() -> str:
    command = get_command(app)
    return "\n".join(command_sections(command.context_class(command, info_name="atelier")))
