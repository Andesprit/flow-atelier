"""`atelier ask` command."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape

from flow_atelier.cli._shared import console
from flow_atelier.cli.commands.run import drive_flow
from flow_atelier.cli.main import app
from flow_atelier.cli.rendering.render import _render_orchestration_msg
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import Conduit, TaskDefinition


@app.command("ask")
def ask_cmd(
    query: str = typer.Argument(
        ...,
        help="Initial question or task to send to the agent.",
    ),
    path: Path = typer.Option(
        ...,
        "--path",
        "-p",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Working directory the agent can inspect and modify.",
    ),
    harness: str = typer.Option(
        "claude-code",
        "--harness",
        help="Agent to talk to, as listed by `atelier list harnesses`.",
    ),
) -> None:
    """Start an interactive conversation with an AI agent in a target directory.

    The agent's replies stream to the terminal. When it asks a question,
    type the answer at the prompt; the same ACP session continues until
    the agent signals that it is finished.

    :param query: initial question or task sent to the agent.
    :param path: working directory passed to the agent's ACP session.
    :param harness: registry name of the agent, without the ``harness:`` prefix.
    """
    if not query.strip():
        console.print("[red]error:[/red] query cannot be empty")
        raise typer.Exit(code=2)

    atelier = Atelier()
    conduit = Conduit(
        name="ask",
        description=f"Interactive {harness} conversation.",
        max_concurrency=1,
        tasks=[
            TaskDefinition(
                name="chat",
                description=f"Ask {harness}",
                task=query,
                tool=f"harness:{harness}",
                depends_on=[],
                interactive=True,
            )
        ],
    )

    problems = atelier.tool_readiness(conduit)
    if problems:
        for problem in problems:
            console.print(f"[red]cannot run:[/red] {escape(problem)}")
        raise typer.Exit(code=1)

    target = path.resolve()
    console.print(_render_orchestration_msg(f'asking {harness} in "{target}"'))
    drive_flow(
        lambda **callbacks: atelier.engine.run(
            conduit, {}, working_dir=target, stoppable=True, **callbacks
        ),
        total_tasks=1,
        resume_hint=False,
    )
