"""`atelier plan` command — render a conduit's static execution plan."""
from __future__ import annotations

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
) -> None:
    """Show a conduit's static execution plan without running anything.

    Validates the conduit first (failing identically to ``atelier check``),
    then renders the DAG as ordered waves with plain/conditional edges, loop
    predicates, sinks, and short-circuit gates. Read-only: no flow is created.

    :param conduit_name: the conduit to render.
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

    render_plan(build_plan(conduit, parsed), console)
