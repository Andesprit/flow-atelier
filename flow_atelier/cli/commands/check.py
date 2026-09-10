"""`atelier check` command — validate conduits without running them."""
from __future__ import annotations

import typer
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import _exit_unknown_conduit, console
from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import ConduitValidationError, validate_conduit


def _check_one(atelier: Atelier, name: str) -> str | None:
    """Validate one conduit fully without executing it.

    Combines structural validation with a readiness probe: even a
    structurally-valid conduit FAILs here if a referenced tool is unregistered
    or its harness CLI is missing from PATH, so authors learn before a run.

    :param atelier: configured :class:`Atelier` providing the store.
    :param name: conduit name to load and validate.
    :returns: ``None`` when valid and runnable, else a one-line failure message.
    """
    try:
        conduit = atelier.store.read_conduit(name)
        validate_conduit(conduit)
    except ValidationError as e:
        first = e.errors()[0]
        return first.get("msg", str(e))
    except (ConduitValidationError, ValueError) as e:
        return str(e)
    problems = atelier.tool_readiness(conduit)
    if problems:
        return "; ".join(problems)
    return None


@app.command("check")
def check_cmd(
    conduit_name: str = typer.Argument(
        None, help="Conduit to check; omit to check all."
    ),
) -> None:
    """Validate hand-authored conduits without running any task.

    :param conduit_name: a single conduit to check; when omitted, all
        project and global conduits are checked.
    """
    atelier = Atelier()
    sources = dict(atelier.store.list_conduits_with_source())

    if conduit_name is not None:
        if conduit_name not in sources:
            _exit_unknown_conduit(conduit_name, list(sources))
        targets = [(conduit_name, sources[conduit_name])]
    else:
        targets = atelier.store.list_conduits_with_source()

    if not targets:
        console.print("[yellow]no conduits found[/yellow]")
        return

    failed = False
    for name, source in targets:
        error = _check_one(atelier, name)
        label = rf"{escape(name)} \[{escape(source)}]"
        if error is None:
            console.print(f"{label} — [green]OK[/green]")
            conduit = atelier.store.read_conduit(name)
            required = [k for k, spec in conduit.inputs.items() if spec.default is None]
            if required:
                keys = ", ".join(escape(k) for k in required)
                console.print(f"    [dim]requires --input: {keys}[/dim]")
        else:
            failed = True
            console.print(f"{label} — [red]FAIL: {escape(error)}[/red]")

    if failed:
        raise typer.Exit(code=1)
