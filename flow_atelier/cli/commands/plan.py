"""`atelier plan` command — render a conduit's static execution plan."""
from __future__ import annotations

import dataclasses
import json

import typer
from pydantic import ValidationError
from rich.markup import escape

from flow_atelier.cli._shared import (
    _exit_unknown_conduit,
    console,
    parse_agents_option,
    parse_worktrees_option,
)
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import format_conduit_error, render_plan
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.binding import BindingError
from flow_atelier.modules.engine import ConduitValidationError, validate_conduit
from flow_atelier.modules.plan import PlanIsolation, build_plan
from flow_atelier.modules.workspace import (
    WorkspaceError,
    check_selectors,
    resolve_source_repo,
)

AGENT_HELP = (
    "TASK=HARNESS: run one top-level agent task on another agent for this "
    "invocation (repeatable). The conduit file is never changed."
)

WORKTREE_HELP = (
    "TASK: preview giving that top-level task its own Git worktree "
    "(repeatable). Nothing is created — plan only reports what run would cut, "
    "and from which commit."
)


@app.command("plan")
def plan_cmd(
    conduit_name: str = typer.Argument(..., help="Conduit to show the plan for."),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of wave blocks."
    ),
    agents_raw: list[str] = typer.Option([], "--agent", help=AGENT_HELP),
    worktrees_raw: list[str] = typer.Option([], "--worktree", help=WORKTREE_HELP),
) -> None:
    """Show a conduit's static execution plan without running anything.

    Validates the conduit first (failing identically to ``atelier check``),
    then renders the DAG as ordered waves with plain/conditional edges, loop
    predicates, sinks, and short-circuit gates. Read-only: no flow is created.

    ``--agent TASK=HARNESS`` previews what ``atelier run`` would do with the
    same flags: every task shows the tool it would actually run on, and a
    replaced one also shows the recipe's own choice. ``--worktree TASK``
    previews the separate Git checkouts a run would cut, naming the source
    repository and the exact commit. Still read-only — no agent is started, no
    flow or worktree is created and the conduit file is not touched.

    :param conduit_name: the conduit to render.
    :param json_mode: when true, emit the plan as JSON instead of wave blocks.
    :param agents_raw: repeated ``TASK=HARNESS`` agent selections to preview.
    :param worktrees_raw: repeated ``TASK`` names to preview isolating.
    """
    atelier = Atelier()
    agents = parse_agents_option(agents_raw)
    worktrees = parse_worktrees_option(worktrees_raw)
    sources = dict(atelier.store.list_conduits_with_source())
    if conduit_name not in sources:
        _exit_unknown_conduit(conduit_name, list(sources))

    try:
        recipe = atelier.store.read_conduit(conduit_name)
        conduit = atelier.bind_agents(recipe, agents)
        parsed = validate_conduit(conduit)
    except BindingError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(code=e.code)
    except (ValidationError, ConduitValidationError, ValueError) as e:
        console.print(f"[red]FAIL: {escape(format_conduit_error(e))}[/red]")
        raise typer.Exit(code=1)

    plan = build_plan(conduit, parsed, recipe=recipe, isolation=_isolation(conduit, worktrees))
    if json_mode:
        typer.echo(json.dumps(dataclasses.asdict(plan), indent=2))
        return
    render_plan(plan, console)


def _isolation(conduit, tasks: list[str]) -> PlanIsolation | None:
    """Describe what ``--worktree`` would do, creating nothing.

    A selector the recipe cannot honour is a usage error and exits; a source
    repository that would refuse the run is reported instead, because seeing
    *why* a run would stop is the point of asking before running.

    :param conduit: the conduit as it would be run.
    :param tasks: the selected top-level task names.
    :returns: the preview, or ``None`` when nothing was selected.
    """
    if not tasks:
        return None
    try:
        check_selectors(conduit, tasks)
    except WorkspaceError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=exc.code) from exc
    try:
        source = resolve_source_repo(None)
    except WorkspaceError as exc:
        return PlanIsolation(tasks=tasks, problem=str(exc))
    return PlanIsolation(tasks=tasks, source=str(source.root), base=source.head)
