"""Trusted driver invoked by the bundled continuous-improvement recipe."""
import asyncio
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import typer

from flow_atelier.cli.main import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.modules.improvement import experiment, propose


@app.command("experiment", hidden=True)
def experiment_cmd(
    run_id: str, project: Path, goal: str, schedule: str,
    proposal_file: Path | None = typer.Option(None, "--proposal-file"),
) -> None:
    """Propose a hypothesis or measure and publish the saved proposal."""
    atelier = Atelier()
    try:
        with redirect_stdout(sys.stderr):
            if proposal_file is None:
                result = asyncio.run(propose(atelier, run_id, project, goal))
            else:
                proposal = json.loads(proposal_file.read_text(encoding="utf-8"))
                result = asyncio.run(experiment(atelier, run_id, project, goal, schedule, proposal))
        typer.echo(json.dumps(result, allow_nan=False))
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
