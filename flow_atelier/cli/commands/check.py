"""`atelier check` command — validate conduits without running them."""
from __future__ import annotations

import json

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
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of text."
    ),
) -> None:
    """Validate hand-authored conduits without running any task.

    :param conduit_name: a single conduit to check; when omitted, all
        project and global conduits are checked.
    :param json_mode: when true, emit one JSON record per conduit.
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
        if json_mode:
            typer.echo("[]")
        else:
            console.print("[yellow]no conduits found[/yellow]")
        return

    rows: list[dict[str, object]] = []
    for name, source in targets:
        error = _check_one(atelier, name)
        required: list[str] = []
        if error is None:
            conduit = atelier.store.read_conduit(name)
            required = [k for k, spec in conduit.inputs.items() if spec.default is None]
        rows.append(
            {
                "name": name,
                "source": source,
                "ok": error is None,
                "error": error,
                "required_inputs": required,
            }
        )

    if json_mode:
        typer.echo(json.dumps(rows, indent=2))
    else:
        for row in rows:
            label = rf"{escape(str(row['name']))} \[{escape(str(row['source']))}]"
            if row["ok"]:
                console.print(f"{label} — [green]OK[/green]")
                if row["required_inputs"]:
                    keys = ", ".join(escape(k) for k in row["required_inputs"])
                    console.print(f"    [dim]requires --input: {keys}[/dim]")
            else:
                console.print(f"{label} — [red]FAIL: {escape(str(row['error']))}[/red]")

    if any(not row["ok"] for row in rows):
        raise typer.Exit(code=1)
