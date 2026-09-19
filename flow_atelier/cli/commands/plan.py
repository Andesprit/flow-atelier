"""`atelier plan` command — render a conduit's static execution plan."""
from __future__ import annotations

import dataclasses
import json

import typer
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import _exit_unknown_conduit, console
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import format_conduit_error, render_plan
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.engine import ConduitValidationError, validate_conduit
from flow_atelier.modules.plan import build_plan


@app.command("plan")
def plan_cmd(
    conduit_name: str = typer.Argument(..., help="Conduit to show the plan for."),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of wave blocks."
    ),
) -> None:
    """Show a conduit's static execution plan without running anything.

    Validates the conduit first (failing identically to ``atelier check``),
    then renders the DAG as ordered waves with plain/conditional edges, loop
    predicates, sinks, and short-circuit gates. Read-only: no flow is created.

    :param conduit_name: the conduit to render.
    :param json_mode: when true, emit the plan as JSON instead of wave blocks.
    """
    atelier = Atelier()
    sources = dict(atelier.store.list_conduits_with_source())
    if conduit_name not in sources:
        _exit_unknown_conduit(conduit_name, list(sources))

    try:
        conduit = atelier.store.read_conduit(conduit_name)
        parsed = validate_conduit(conduit)
    except (ValidationError, ConduitValidationError, ValueError) as e:
        console.print(f"[red]FAIL: {escape(format_conduit_error(e))}[/red]")
        raise typer.Exit(code=1)

    plan = build_plan(conduit, parsed)
    if json_mode:
        typer.echo(json.dumps(dataclasses.asdict(plan), indent=2))
        return
    render_plan(plan, console)
